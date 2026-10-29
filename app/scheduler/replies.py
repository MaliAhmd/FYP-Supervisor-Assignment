"""Persisted, overlapping Gmail scans. Only matched replies become project data."""
from datetime import datetime
from hashlib import sha256
import json
import time

from app.db.repository import new_id
from app.gateway.prompts import digest
from app.gateway.service import _WorkerLock
from app.gateway.types import GatewayError
from app.integrations.gmail.reader import MailReadError
from app.scheduler.polling import PollingScheduler


class ReplyPoller:
    def __init__(self, db, settings, reader, *, clock=time.time):
        self.db, self.settings, self.reader, self.clock = db, settings, reader, clock

    def ingest(self, pid, message):
        refs = set(message.references)
        if message.in_reply_to:
            refs.add(message.in_reply_to)
        if not refs:
            return False
        with self.db.transaction() as conn:
            # Supervisor-level matcher: detect references spanning projects, never
            # expose these records to an agent or import an ambiguous message.
            marks = ','.join('?' for _ in refs)
            outgoing = conn.execute(f"SELECT project_id,actual_to,delivery_mode FROM email_drafts WHERE message_id IN ({marks}) AND (status='sent' OR delivery_uncertain=1)", tuple(refs)).fetchall()
            if {r['project_id'] for r in outgoing} != {pid}:
                return False
            project = conn.execute('SELECT student_email FROM projects WHERE id=?', (pid,)).fetchone()
            if not project:
                raise LookupError('Project not found')
            accepted = {r['actual_to'] if r['delivery_mode'] == 'test' else project['student_email'] for r in outgoing}
            if message.sender.casefold() not in {a.casefold() for a in accepted if a}:
                return False
            existing = conn.execute('SELECT project_id FROM email_inbound WHERE gmail_message_id=?', (message.gmail_id,)).fetchone()
            if existing:
                return False
            conn.execute("""INSERT INTO email_inbound(id,project_id,gmail_message_id,in_reply_to,references_header,received_at,body_hash,body,sender,message_id,thread_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (new_id(), pid, message.gmail_id, message.in_reply_to, ' '.join(message.references), message.received_at,
                    sha256(message.body.encode()).hexdigest(), message.body, message.sender, message.message_id, message.thread_id))
            after = 'email:' + message.gmail_id
            key = sha256(json.dumps([pid, after]).encode()).hexdigest()
            conn.execute("INSERT INTO cycles(id,project_id,trigger,after_sha,idempotency_key) VALUES (?,?,'email_reply',?,?) ON CONFLICT(idempotency_key) DO NOTHING", (new_id(), pid, after, key))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'email.reply_received','application',?)", (pid, json.dumps({'gmail_id': message.gmail_id})))
        return True

    def poll_project(self, pid):
        try:
            lock = _WorkerLock(self.db.path.with_suffix('.replies-' + digest(pid) + '.lock'))
        except GatewayError:
            return {'status': 'busy'}
        try:
            return self._poll(pid)
        finally:
            lock.close()

    def _poll(self, pid):
        now = self.clock()
        with self.db.transaction() as conn:
            project = conn.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
            if not project:
                raise LookupError('Project not found')
            sent = conn.execute("SELECT actual_to,delivery_mode,coalesce(sent_at,send_started_at) AS since FROM email_drafts WHERE project_id=? AND (status='sent' OR delivery_uncertain=1)", (pid,)).fetchall()
            if not sent or project['status'] == 'paused':
                return {'status': 'idle'}
            conn.execute('INSERT INTO gmail_poll_state(project_id) VALUES (?) ON CONFLICT DO NOTHING', (pid,))
            state = dict(conn.execute('SELECT * FROM gmail_poll_state WHERE project_id=?', (pid,)).fetchone())
            if state['next_poll'] > now:
                return {'status': 'waiting'}
            if state['scan_end'] is None:
                earliest = min(int(datetime.fromisoformat(r['since'].replace('Z', '+00:00')).timestamp()) for r in sent if r['since'])
                state.update(scan_start=max(0, (state['watermark'] or earliest) - 86400), scan_end=int(now) + 1, page_token=None)
                addresses = {project['student_email']} | {r['actual_to'] for r in sent if r['delivery_mode'] == 'test' and r['actual_to']}
                state['scan_query'] = '{' + ' '.join('from:"' + a + '"' for a in sorted(addresses)) + '} after:' + str(state['scan_start']) + ' before:' + str(state['scan_end'])
                conn.execute('UPDATE gmail_poll_state SET scan_start=?,scan_end=?,scan_query=?,page_token=NULL WHERE project_id=?', (state['scan_start'], state['scan_end'], state['scan_query'], pid))
        # Page tokens always use their original query, even if configuration changes.
        query = state['scan_query']
        imported = 0
        try:
            for _ in range(self.settings.gmail_pages_per_tick):
                ids, token = self.reader.list_page(query=query, page_token=state['page_token'])
                for key in ids:
                    with self.db.transaction(write=False) as conn:
                        existing = conn.execute('SELECT 1 FROM email_inbound WHERE project_id=? AND gmail_message_id=?', (pid, key)).fetchone()
                    if existing:
                        continue
                    try:
                        incoming = self.reader.read(key)
                    except MailReadError as error:
                        if str(error) not in {'gmail_invalid_message', 'gmail_message_too_large', 'gmail_no_text'}:
                            raise
                        with self.db.transaction() as conn:
                            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'email.reply_skipped','application',?)", (pid, json.dumps({'gmail_id': key, 'reason': str(error)})))
                        continue
                    imported += int(self.ingest(pid, incoming))
                if token and token == state['page_token']:
                    raise MailReadError()
                with self.db.transaction() as conn:
                    if not token:
                        conn.execute('UPDATE gmail_poll_state SET watermark=?,scan_start=NULL,scan_end=NULL,scan_query=NULL,page_token=NULL,next_poll=?,failures=0,last_error=NULL WHERE project_id=?', (state['scan_end'] - 1, now + self.settings.reply_poll_seconds, pid))
                    else:
                        conn.execute('UPDATE gmail_poll_state SET page_token=?,next_poll=?,failures=0,last_error=NULL WHERE project_id=?', (token, now + self.settings.reply_poll_seconds, pid))
                if not token:
                    return {'status': 'complete', 'imported': imported}
                state['page_token'] = token
            return {'status': 'more_pages', 'imported': imported}
        except Exception:
            with self.db.transaction() as conn:
                failures = state['failures'] + 1
                conn.execute('UPDATE gmail_poll_state SET next_poll=?,failures=?,last_error=? WHERE project_id=?', (now + min(3600, self.settings.reply_poll_seconds * 2 ** min(failures, 6)), failures, 'gmail_read_failed', pid))
            return {'status': 'deferred', 'reason': 'gmail_read_failed', 'imported': imported}

    def tick(self):
        with self.db.transaction(write=False) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM projects WHERE status!='paused' ORDER BY id")]
        return [self.poll_project(pid) for pid in ids]

    def background(self):
        return PollingScheduler(self, interval_seconds=self.settings.reply_poll_seconds)
