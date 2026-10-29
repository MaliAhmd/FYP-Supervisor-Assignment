"""Durable bounded review controller. step() advances at most one stage.

No model output invokes tools. File locks are released by the OS on process death;
SQLite commits pair output writes with the next checkpoint.
"""
from hashlib import sha256
import json
import re

from app.agents.review import review_specs
from app.db import ProjectStore
from app.db.repository import new_id
from app.gateway.prompts import spec_hash
from app.gateway.service import _WorkerLock
from app.gateway.types import BudgetExceeded, GatewayError
from app.integrations.github.changes import read_changes
from app.integrations.github.models import GitHubError
from app.integrations.pdf.extraction import PDFError
from app.integrations.pdf.retrieval import Retriever
from app.orchestrator.deadlines import DeadlineService
from app.orchestrator.triage import classification, formatting_only
from app.orchestrator.verification import save_claims, save_review
from app.orchestrator.batching import pack_repository, fits


STAGES = ('load_memory', 'collect', 'coordinator', 'repo_reviewer', 'report_reviewer', 'analyze_reply', 'draft_followup', 'finalize')


class ReviewEngine:
    def __init__(self, db, gateway, github, pdfs):
        self.db, self.gateway, self.github, self.pdfs = db, gateway, github, pdfs
        self.settings = gateway.settings
        for spec in review_specs():
            if spec.name not in gateway.specs or spec_hash(gateway.specs[spec.name]) != spec_hash(spec):
                raise ValueError('Register the review agent specifications at gateway startup')

    def status(self, pid, cid):
        with self.db.transaction(write=False) as conn:
            row = conn.execute('SELECT c.*,r.reason,r.payload,r.batch_id FROM cycles c LEFT JOIN review_state r ON r.project_id=c.project_id AND r.cycle_id=c.id WHERE c.project_id=? AND c.id=?', (pid, cid)).fetchone()
        if row is None:
            raise LookupError('Cycle not found for project')
        value = dict(row)
        value['payload'] = json.loads(value['payload'] or '{}')
        if value['status'] == 'pending_evidence_review' and not value['reason']:
            job = value['payload'].get('jobs', {}).get(value['stage'])
            if job:
                value['reason'] = self.gateway.result(pid, job).reason
        return value

    def _lock(self, pid):
        # Hash avoids using caller-controlled project IDs as filesystem paths.
        return _WorkerLock(self.db.path.with_suffix('.review-' + sha256(pid.encode()).hexdigest() + '.lock'))

    def step(self, pid, cid, *, batch_id):
        if not batch_id or len(batch_id) > 200:
            raise ValueError('An explicit bounded batch ID is required')
        try:
            lock = self._lock(pid)
        except GatewayError:
            return {**self.status(pid, cid), 'busy': True}
        try:
            row = self.status(pid, cid)
            if row['status'] in {'completed', 'skipped', 'failed', 'pending_evidence_review'}:
                return row
            project = ProjectStore(self.db, pid).project()
            if not project['baseline_confirmed_at'] or project['status'] == 'paused':
                return {**row, 'blocked': 'project_not_ready'}
            if re.fullmatch(r'[0-9a-fA-F]{40}', row['after_sha']) and project['status'] != 'active':
                return {**row, 'blocked': 'repository_access_not_active'}
            if row['trigger'] == 'proposal_upload':
                return {**row, 'blocked': 'baseline_service_owns_cycle'}
            with self.db.transaction() as conn:
                # Retain one logical owner while waiting for the gateway or quota.
                other = conn.execute("SELECT id FROM cycles WHERE project_id=? AND id!=? AND status IN ('running','pending_evidence_review') LIMIT 1", (pid, cid)).fetchone()
                if other:
                    return {**row, 'blocked': 'another_review_unfinished'}
                conn.execute('INSERT INTO review_state(project_id,cycle_id,batch_id) VALUES (?,?,?) ON CONFLICT DO NOTHING', (pid, cid, batch_id))
                conn.execute("UPDATE cycles SET status='running',started_at=coalesce(started_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE project_id=? AND id=?", (pid, cid))
            row = self.status(pid, cid)
            try:
                self._advance(pid, cid, row, project)
            except (BudgetExceeded, GitHubError, PDFError) as error:
                self._pending(pid, cid, str(error))
            except (ValueError, LookupError) as error:
                current = self.status(pid, cid)
                job = current['payload'].get('jobs', {}).get(current['stage'])
                if str(error) in {'invalid_finding_evidence', 'invalid_feedback_evidence'} and job:
                    if not self.gateway.repair_output(pid, job):
                        self._pending(pid, cid, 'invalid_review_evidence')
                else:
                    self._pending(pid, cid, 'invalid_review_data')
            except Exception:
                # Never include credentials, raw repository text or provider errors.
                self._pending(pid, cid, 'review_internal_error')
            return self.status(pid, cid)
        finally:
            lock.close()

    def resume(self, pid, cid, *, batch_id=None):
        if batch_id is not None and (not batch_id or len(batch_id) > 200):
            raise ValueError('Invalid batch ID')
        lock = self._lock(pid)
        try:
            row = self.status(pid, cid)
            if row['status'] != 'pending_evidence_review':
                raise ValueError('Only a pending review can resume')
            state = row['payload']
            if row['reason'] == 'review_batch_complete':
                # An explicit supervisor action starts a NEW bounded cycle.
                # Previous attempts, daily quota and the shared batch are retained.
                target = new_id()
                group = state.setdefault('review_cycles', [cid])
                group.append(target)
                with self.db.transaction() as conn:
                    conn.execute("UPDATE cycles SET status='completed',stage='batch_complete',finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND id=?", (pid, cid))
                    conn.execute('INSERT INTO cycles(id,project_id,trigger,before_sha,after_sha,idempotency_key,stage) VALUES (?,?,?,?,?,?,?)',
                        (target, pid, 'manual', row['before_sha'], row['after_sha'], 'continuation:' + cid, row['stage']))
                    conn.execute('INSERT INTO review_state(project_id,cycle_id,batch_id,payload) VALUES (?,?,?,?)',
                        (pid, target, batch_id or row['batch_id'], json.dumps(state)))
                    old = dict(state, next_cycle=target)
                    self._save(conn, pid, cid, 'batch_complete', old)
                    conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'review.continued','supervisor',?)", (pid, json.dumps({'previous_cycle':cid,'cycle_id':target})))
                return target
            job = row['payload'].get('jobs', {}).get(row['stage'])
            # Upgrade the saved oversized request without resetting any attempts.
            # Recollect legacy truncated evidence with the new lossless slices.
            if job and row['stage'] == 'repo_reviewer' and self.gateway.result(pid, job).reason == 'input_too_large' and 'repo_batches' not in state:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE gateway_jobs SET status='failed',reason='replaced_by_repository_batches' WHERE project_id=? AND id=? AND status='pending'", (pid, job))
                    state['jobs'].pop('repo_reviewer', None)
                    self._save(conn, pid, cid, 'collect' if state['change']['limited'] else 'repo_reviewer', state)
                job = None
            if job:
                result = self.gateway.result(pid, job)
                if result.status == 'pending':
                    self.gateway.resume(pid, job, batch_id=batch_id)
                elif result.status == 'failed':
                    raise ValueError('Failed job requires supervisor intervention')
            with self.db.transaction() as conn:
                conn.execute("UPDATE cycles SET status='queued' WHERE project_id=? AND id=?", (pid, cid))
                conn.execute('UPDATE review_state SET reason=NULL WHERE project_id=? AND cycle_id=?', (pid, cid))
                if batch_id:
                    conn.execute('UPDATE review_state SET batch_id=? WHERE project_id=? AND cycle_id=?', (batch_id, pid, cid))
            return cid
        finally:
            lock.close()

    def _pending(self, pid, cid, reason):
        with self.db.transaction() as conn:
            conn.execute("UPDATE cycles SET status='pending_evidence_review' WHERE project_id=? AND id=?", (pid, cid))
            conn.execute('UPDATE review_state SET reason=? WHERE project_id=? AND cycle_id=?', (reason, pid, cid))

    def _save(self, conn, pid, cid, stage, state):
        payload = json.dumps(state)
        conn.execute('UPDATE review_state SET payload=?,reason=NULL WHERE project_id=? AND cycle_id=?', (payload, pid, cid))
        conn.execute('UPDATE cycles SET stage=? WHERE project_id=? AND id=?', (stage, pid, cid))
        conn.execute('INSERT INTO checkpoints(project_id,cycle_id,stage,payload) VALUES (?,?,?,?) ON CONFLICT(project_id,cycle_id,stage) DO UPDATE SET payload=excluded.payload', (pid, cid, stage, payload))

    def _tool(self, pid, cid, agent, name, callback):
        return self.gateway.ledger.run_tool(pid, cid, self.gateway.specs[agent], name, callback)

    def _memory(self, pid):
        with self.db.transaction(write=False) as conn:
            milestones = [dict(r) for r in conn.execute("SELECT m.id,m.completion_criteria,d.due_date FROM milestones m LEFT JOIN deadline_versions d ON d.project_id=m.project_id AND d.milestone_id=m.id AND d.status='approved' WHERE m.project_id=? ORDER BY m.id", (pid,))]
            feedback = [dict(r) for r in conn.execute("SELECT id,text,status FROM feedback_items WHERE project_id=? AND status!='resolved' ORDER BY id", (pid,))]
            claims = [dict(r) for r in conn.execute('SELECT id,text,status FROM claims WHERE project_id=? ORDER BY rowid DESC', (pid,))]
        return {'milestones': milestones, 'feedback': feedback, 'claims': claims, 'delayed': DeadlineService(self.db).delayed(pid)}

    def _collect(self, pid, cid, row, project):
        store = ProjectStore(self.db, pid)
        result = {'repo': [], 'reports': [], 'emails': [], 'inbound': [], 'ignored': [], 'limited': False, 'history_rewritten': False}
        if re.fullmatch(r'[0-9a-fA-F]{40}', row['after_sha']):
            # Always start at authoritative reviewed memory, not a stale queued base.
            before = project['last_reviewed_sha']
            changes = read_changes(self.github, project['repo_full_name'], before, row['after_sha'])
            result.update(history_rewritten=changes.history_rewritten, change_mode=changes.mode, change_reason=changes.reason, before_sha=before)
            old_files = None
            for file in changes.files:
                path = file.get('filename', file.get('path', ''))
                kind = classification(path)
                if kind in {'ignored', 'readme'} or file.get('type', 'blob') != 'blob':
                    result['ignored'].append(path)
                    continue
                deleted = file.get('status') == 'removed'
                if kind == 'pdf' and not deleted:
                    document = self.pdfs.ingest(pid, self.github.blob(project['repo_full_name'], file['sha']), kind='report', filename=path, cycle_id=cid)
                    result['reports'].append(document['id'])
                    continue
                patch = file.get('patch')
                # Prefer complete sources for safe formatting detection.
                content = b'' if deleted else self.github.blob(project['repo_full_name'], file['sha'])
                if b'\x00' in content:
                    result['ignored'].append(path)
                    continue
                try:
                    text = content.decode('utf-8')
                except UnicodeDecodeError:
                    result['ignored'].append(path)
                    continue
                if changes.mode == 'diff' and path.endswith('.py') and not deleted and file.get('status') == 'modified':
                    if old_files is None:
                        commit = self.github.commit(project['repo_full_name'], before)
                        old_files = {f['path']: f for f in self.github.tree(project['repo_full_name'], commit['tree']['sha'])}
                    old = old_files.get(path)
                    if old and formatting_only(path, self.github.blob(project['repo_full_name'], old['sha']), content):
                        result['ignored'].append(path)
                        continue
                text = patch if patch is not None else ('DELETED: ' + path if deleted else text)
                size = self.settings.max_diff_chars_per_file
                for start in range(0, max(1, len(text)), size):
                    end = min(len(text), start + size)
                    ref = f"git:{row['after_sha']}:{path}"
                    meta = {'path': path, 'sha': file['sha'], 'mode': changes.mode, 'limited': False}
                    if len(text) > size:
                        ref += f':chars:{start}:{end}'
                        meta.update(start=start, end=end, total_chars=len(text))
                    result['repo'].append(store.add_evidence(cid, 'file_diff', ref, text[start:end], meta))
        elif row['trigger'] not in {'report_upload', 'email_reply'}:
            raise ValueError('Unsupported cycle trigger')
        with self.db.transaction(write=False) as conn:
            docs = conn.execute("SELECT id FROM pdf_documents WHERE project_id=? AND kind='report' AND cycle_id=?", (pid, cid)).fetchall()
            candidates = set(result['reports']) | {r[0] for r in docs}
            result['reports'] = sorted(key for key in candidates if not conn.execute('SELECT 1 FROM reviewed_documents WHERE project_id=? AND document_id=?', (pid, key)).fetchone())
            emails = conn.execute('SELECT * FROM email_inbound WHERE project_id=? AND processed=0 ORDER BY received_at,id', (pid,)).fetchall()
        for email in emails:
            result['emails'].append(store.add_evidence(cid, 'email', 'gmail:' + email['gmail_message_id'], email['body']))
            result['inbound'].append(email['id'])
        return result

    def _advance(self, pid, cid, row, project):
        stage, state = row['stage'], row['payload']
        if stage == 'load_memory':
            state['memory'] = self._tool(pid, cid, 'coordinator', 'load_memory', lambda: self._memory(pid))
        elif stage == 'collect':
            state['change'] = self._tool(pid, cid, 'repo_reviewer', 'read_diff', lambda: self._collect(pid, cid, row, project))
            change = state['change']
            available = []
            if change['repo']: available.append('review_repo')
            if change['reports']: available.append('review_report')
            if change['emails']: available.append('analyze_reply')
            if state['memory']['feedback'] and (change['repo'] or change['reports']): available.append('check_prior_feedback')
            if available: available.append('draft_followup')
            state['available'] = available
            if not available:
                state['skip'] = True
                with self.db.transaction() as conn:
                    self._save(conn, pid, cid, 'finalize', state)
                return
        elif stage == 'finalize':
            self._finalize(pid, cid, row, state)
            return
        else:
            self._agent_stage(pid, cid, row, state)
            return
        with self.db.transaction() as conn:
            self._save(conn, pid, cid, STAGES[STAGES.index(stage) + 1], state)

    def _input(self, pid, cid, stage, state):
        change, memory = state['change'], state['memory']
        if stage == 'coordinator':
            return {'available': state['available'], 'counts': {k: len(change[k]) for k in ('repo', 'reports', 'emails')}, 'open_feedback': len(memory['feedback']), 'history_rewritten': change['history_rewritten']}, []
        if stage == 'repo_reviewer':
            ids = change['repo']
            feedback = self._tool(pid, cid, 'repo_reviewer', 'check_feedback', lambda: memory['feedback'])
            return {'criteria': memory['milestones'], 'feedback': feedback}, ids
        if stage == 'report_reviewer':
            query = ' '.join(m['completion_criteria'] for m in memory['milestones'])
            passages = self._tool(pid, cid, 'report_reviewer', 'retrieve_passages', lambda: [p.as_dict() for doc in change['reports'] for p in Retriever(self.pdfs).retrieve(pid, doc, query)])
            if not passages:
                raise ValueError('No relevant report text')
            passages = sorted(passages, key=lambda p: (-p['score'], p['document_id'], p['page'], p['start']))[:self.settings.report_top_k]
            state['report_passages'] = passages
            feedback = self._tool(pid, cid, 'report_reviewer', 'check_feedback', lambda: memory['feedback'])
            # Only passage text is sent; never send the entire PDF page again.
            return {'criteria': memory['milestones'], 'feedback': feedback, 'passages': passages}, []
        if stage == 'analyze_reply':
            ids = self._tool(pid, cid, 'claims_comms', 'read_evidence', lambda: change['emails'] + change['repo'])
            return {'mode': 'analyze', 'criteria': memory['milestones'], 'prior_claims': memory['claims'], 'passages': state.get('report_passages', [])}, ids
        with self.db.transaction(write=False) as conn:
            group = state.get('review_cycles', [cid])
            marks = ','.join('?' for _ in group)
            findings = [dict(r) for r in conn.execute(f'SELECT text,severity FROM findings WHERE project_id=? AND cycle_id IN ({marks}) ORDER BY rowid', (pid, *group))]
            claims = [dict(r) for r in conn.execute(f'SELECT c.text,c.status FROM claims c JOIN claim_verifications v ON v.project_id=c.project_id AND v.claim_id=c.id WHERE v.project_id=? AND v.cycle_id IN ({marks})', (pid, *group))]
        data = {'mode': 'draft', 'findings': findings, 'claims': claims, 'delayed': DeadlineService(self.db).delayed(pid), 'history_rewritten': change['history_rewritten'], 'open_feedback': memory['feedback']}
        if len(state.get('repo_batches', [])) > 1:
            data['total_findings'] = len(findings)
            data['omitted_findings'] = 0
            while data['findings'] and not fits(self.gateway, pid, cid, self.status(pid, cid)['batch_id'], 'claims_comms', data, []):
                data['findings'].pop()
                data['omitted_findings'] += 1
            state['draft_omitted_findings'] = data['omitted_findings']
        return data, []

    def _agent_stage(self, pid, cid, row, state):
        stage = row['stage']
        if stage == 'repo_reviewer' and 'review_repo' in state['available']:
            self._repository_batch(pid, cid, row, state)
            return
        action = {'repo_reviewer': 'review_repo', 'report_reviewer': 'review_report', 'analyze_reply': 'analyze_reply', 'draft_followup': 'draft_followup'}.get(stage)
        # Required evidence work is deterministic; the coordinator cannot suppress it.
        if action and action not in state['available']:
            with self.db.transaction() as conn:
                self._save(conn, pid, cid, STAGES[STAGES.index(stage)+1], state)
            return
        agent = 'claims_comms' if stage in {'analyze_reply', 'draft_followup'} else stage
        jobs = state.setdefault('jobs', {})
        if stage not in jobs:
            if len(state.get('repo_batches', [])) > 1 and self._needs_continuation(pid, cid, row, agent):
                return
            data, ids = self._input(pid, cid, stage, state)
            allowed = list(ids)
            if stage == 'report_reviewer':
                allowed = list({p['evidence_id'] for p in data['passages']})
            elif stage == 'analyze_reply':
                allowed += list({p['evidence_id'] for p in data['passages']})
            state.setdefault('allowed', {})[stage] = allowed
            with self.db.transaction() as conn:
                jobs[stage] = self.gateway.submit(pid, cid, row['batch_id'], agent, data=data, evidence_ids=ids, connection=conn)
                self._save(conn, pid, cid, stage, state)
            return
        result = self.gateway.result(pid, jobs[stage])
        if result.status in {'pending', 'failed'}:
            self._pending(pid, cid, result.reason or 'agent_failed')
            return
        if result.status != 'completed':
            return
        output = result.value
        with self.db.transaction() as conn:
            if stage == 'coordinator':
                requested = set(output.actions)
                if ('no_action' in requested and len(requested) != 1) or not requested <= set(state['available']) | {'no_action'}:
                    raise ValueError('Invalid plan')
                state['proposed_actions'] = output.actions
                state['effective_actions'] = state['available']
            elif stage in {'repo_reviewer', 'report_reviewer'}:
                save_review(conn, pid, cid, output, state['allowed'][stage], {f['id'] for f in state['memory']['feedback']})
            elif stage == 'analyze_reply':
                save_claims(conn, pid, cid, output.claims, state['change']['emails'], state['allowed'][stage])
                state['verification_narrative'] = output.narrative
            elif stage == 'draft_followup':
                if output.claims or not output.subject.strip() or not output.body.strip():
                    raise ValueError('Invalid draft')
                key = sha256(json.dumps([pid, cid, 'followup']).encode()).hexdigest()
                body = output.body
                if state.get('draft_omitted_findings'):
                    body += '\n\nThis message summarizes selected findings. Additional findings are recorded in the supervisor review and need separate follow-up.'
                conn.execute("""INSERT INTO email_drafts(id,project_id,cycle_id,kind,to_addr,subject,body,message_id,idempotency_key)
                    VALUES (?,?,?,'followup',?,?,?,?,?) ON CONFLICT(project_id,cycle_id,kind) DO NOTHING""",
                    (new_id(), pid, cid, ProjectStore(self.db, pid).project()['student_email'], output.subject, body, f'<{key}@supervisorai.local>', key))
            self._save(conn, pid, cid, STAGES[STAGES.index(stage)+1], state)

    def _needs_continuation(self, pid, cid, row, agent):
        remaining = self.gateway.ledger.remaining({'project_id':pid, 'cycle_id':cid, 'batch_id':row['batch_id'], 'agent':agent})
        if remaining['cycle_calls'] < min(self.settings.max_llm_calls_per_cycle, 1 + self.settings.json_repair_retries):
            self._pending(pid, cid, 'review_batch_complete')
            return True
        return False

    def _repository_batch(self, pid, cid, row, state):
        if 'repo_batches' not in state:
            data, ids = self._input(pid, cid, 'repo_reviewer', state)
            state['repo_batches'] = pack_repository(self.gateway, pid, cid, row['batch_id'], data, ids)
            state['repo_batch_data'] = data
            state['repo_batch_index'] = 0
            state['review_cycles'] = [cid]
            with self.db.transaction() as conn:
                self._save(conn, pid, cid, 'repo_reviewer', state)
        index, batches = state['repo_batch_index'], state['repo_batches']
        if index == len(batches):
            with self.db.transaction() as conn:
                self._save(conn, pid, cid, 'report_reviewer', state)
            return
        jobs = state.setdefault('jobs', {})
        if 'repo_reviewer' not in jobs:
            if len(batches) > 1 and self._needs_continuation(pid, cid, row, 'repo_reviewer'):
                return
            with self.db.transaction() as conn:
                jobs['repo_reviewer'] = self.gateway.submit(pid, cid, row['batch_id'], 'repo_reviewer',
                    data=state['repo_batch_data'], evidence_ids=batches[index], connection=conn)
                self._save(conn, pid, cid, 'repo_reviewer', state)
            return
        result = self.gateway.result(pid, jobs['repo_reviewer'])
        if result.status in {'pending', 'failed'}:
            self._pending(pid, cid, result.reason or 'agent_failed')
        elif result.status == 'completed':
            with self.db.transaction() as conn:
                save_review(conn, pid, cid, result.value, batches[index], {f['id'] for f in state['memory']['feedback']})
                state['repo_batch_index'] += 1
                state.setdefault('repo_batch_jobs', []).append(jobs.pop('repo_reviewer'))
                self._save(conn, pid, cid, 'repo_reviewer', state)

    def _finalize(self, pid, cid, row, state):
        if state['change']['limited']:
            self._pending(pid, cid, 'truncated_evidence')
            return
        with self.db.transaction() as conn:
            for doc in state['change']['reports']:
                conn.execute('INSERT INTO reviewed_documents(project_id,document_id,cycle_id) VALUES (?,?,?) ON CONFLICT DO NOTHING', (pid, doc, cid))
            for email in state['change']['inbound']:
                conn.execute('UPDATE email_inbound SET processed=1 WHERE project_id=? AND id=?', (pid, email))
            if re.fullmatch(r'[0-9a-fA-F]{40}', row['after_sha']):
                conn.execute('UPDATE projects SET last_reviewed_sha=? WHERE id=?', (row['after_sha'], pid))
                conn.execute('UPDATE cycles SET before_sha=? WHERE project_id=? AND id=?', (state['change'].get('before_sha'), pid, cid))
            status = 'skipped' if state.get('skip') else 'completed'
            conn.execute("UPDATE cycles SET status=?,stage='done',finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND id=?", (status, pid, cid))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'review.finished','application',?)", (pid, json.dumps({'cycle_id': cid, 'status': status, 'history_rewritten': state['change']['history_rewritten']})))
