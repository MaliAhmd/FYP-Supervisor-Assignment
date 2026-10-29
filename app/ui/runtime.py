"""Durable UI actions and background work. No Streamlit calls from worker threads."""
import atexit
from datetime import date
import json
import threading

from app.agents.fake import FakeReviewClient
from app.agents.review import all_specs
from app.config import validate_email
from app.db import Database, ProjectStore
from app.db.repository import new_id
from app.gateway import Gateway
from app.gateway.service import _WorkerLock
from app.integrations.github.access import AccessService
from app.integrations.github.client import GitHubClient
from app.integrations.github.fake import FakeGitHubClient
from app.integrations.github.invitations import accept_repository_invitation
from app.integrations.pdf.store import PDFStore
from app.orchestrator.baseline import BaselineService
from app.orchestrator.email import EmailService, configured_sender
from app.orchestrator.review import ReviewEngine
from app.scheduler.polling import PollingTrigger


KINDS = {'proposal', 'extract', 'report', 'discover', 'accept', 'verify', 'review', 'resume', 'send', 'replies', 'sheets', 'fake_push', 'google_check', 'export', 'smtp_check'}


class Runtime:
    def __init__(self, settings, *, start=True):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.db.migrate()
        self._lease = _WorkerLock(self.db.path.with_suffix('.ui.lock'))
        self._stop, self._execution = threading.Event(), threading.Lock()
        self._thread = None
        self.closed = False
        self.last_error = None
        self._reply_poller = None
        try:
            self.gateway = Gateway(self.db, settings, all_specs(), client=FakeReviewClient() if settings.llm_mode == 'fake' else None, start=start)
            self.github = FakeGitHubClient() if settings.github_mode == 'fake' else GitHubClient(settings)
            self.pdfs = PDFStore(self.db, settings)
            self.baselines = BaselineService(self.db, self.pdfs, self.gateway)
            self.engine = ReviewEngine(self.db, self.gateway, self.github, self.pdfs)
            accept = self.github.accept_invitation if settings.github_mode == 'fake' else lambda key: accept_repository_invitation(settings, key)
            self.access = AccessService(self.db, self.github, accept)
            self.poller = PollingTrigger(self.db, settings, self.github)
            with self.db.transaction() as conn:
                # Never replay a potentially accepted invitation, send or remote write.
                conn.execute("UPDATE ui_tasks SET status='interrupted',error='interrupted_check_saved_state' WHERE status='running'")
            if start:
                self._thread = threading.Thread(target=self._loop, name='supervisorai-ui-work', daemon=True)
                self._thread.start()
        except BaseException:
            if hasattr(self, 'gateway'): self.gateway.close()
            self._lease.close()
            raise

    def close(self):
        if self.closed: return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=120)
            if self._thread.is_alive(): raise RuntimeError('Worker is still stopping')
        self.gateway.close()
        self.github.close()
        self._lease.close()
        self.closed = True

    def submit(self, pid, kind, payload=None, blob=None):
        if kind not in KINDS: raise ValueError('Unknown action')
        ProjectStore(self.db, pid)
        if blob is not None and (not isinstance(blob, bytes) or len(blob) > self.settings.max_upload_bytes):
            raise ValueError('Upload exceeds configured limit')
        with self.db.transaction() as conn:
            previous = conn.execute("SELECT id FROM ui_tasks WHERE project_id=? AND kind=? AND status IN ('queued','running')", (pid, kind)).fetchone()
            if previous: return previous[0]
            key = new_id()
            conn.execute('INSERT INTO ui_tasks(id,project_id,kind,payload,input_blob) VALUES (?,?,?,?,?)', (key, pid, kind, json.dumps(payload or {}), blob))
        return key

    def monitoring(self, pid, enabled, batch_id, *, replies=None):
        ProjectStore(self.db, pid)
        if not batch_id.strip() or len(batch_id) > 200: raise ValueError('Provide a batch identifier of at most 200 characters')
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO ui_controls(project_id,monitoring,batch_id) VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET monitoring=excluded.monitoring,batch_id=excluded.batch_id,last_error=NULL', (pid, int(enabled), batch_id))
            if replies is not None:
                conn.execute('UPDATE ui_controls SET replies_enabled=? WHERE project_id=?', (int(replies), pid))

    def edit_project(self, pid, name, email, *, paused, repo=None):
        if not name.strip(): raise ValueError('Student name is required')
        email = validate_email(email)
        with self.db.transaction() as conn:
            project = conn.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
            if not project: raise LookupError('Project not found')
            from app.integrations.github.models import repo_name
            repo = repo_name(repo or project['repo_full_name'])
            if repo != project['repo_full_name'] and project['baseline_confirmed_at']:
                raise ValueError('Repository is fixed after baseline confirmation')
            if conn.execute("SELECT 1 FROM cycles WHERE project_id=? AND status='running'", (pid,)).fetchone():
                raise ValueError('Wait for the running review before editing the project')
            if conn.execute('SELECT 1 FROM email_drafts WHERE project_id=? AND delivery_uncertain=1', (pid,)).fetchone():
                raise ValueError('Resolve the delivery attempt before editing the project')
            status = 'paused' if paused else ('pending_access' if project['status'] == 'paused' else project['status'])
            conn.execute('UPDATE projects SET student_name=?,student_email=?,status=?,repo_full_name=? WHERE id=?', (name.strip(), email, status, repo, pid))
            if email != project['student_email']:
                conn.execute("UPDATE email_drafts SET to_addr=?,status='pending',approved_at=NULL,approval_hash=NULL,revision=revision+1 WHERE project_id=? AND status!='sent' AND send_started_at IS NULL", (email, pid))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'project.edited','supervisor','{}')", (pid,))

    def _fake_repo(self, pid):
        if self.settings.github_mode != 'fake': return
        repo = ProjectStore(self.db, pid).project()['repo_full_name']
        if repo not in self.github.repos:
            self.github.add_repository(repo)
            with self.db.transaction(write=False) as conn:
                history = conn.execute('SELECT files_json FROM ui_fake_commits WHERE project_id=? ORDER BY sequence', (pid,)).fetchall()
            for row in history: self.github.publish(repo, json.loads(row[0]))

    def _action(self, task):
        pid, kind, data = task['project_id'], task['kind'], json.loads(task['payload'])
        self._fake_repo(pid)
        batch = data.get('batch_id', 'ui-' + date.today().isoformat())
        if kind == 'proposal': return self.baselines.upload(pid, task['input_blob'], filename=data['filename'])
        if kind == 'report': return self.pdfs.ingest(pid, task['input_blob'], kind='report', filename=data['filename'])
        if kind == 'extract': return self.baselines.request_extraction(pid, data['id'], batch)
        if kind == 'discover': return self.access.discover(pid)
        if kind == 'accept': return self.access.accept(pid, int(data['id']))
        if kind == 'verify': return self.access.refresh_access(pid)
        if kind == 'fake_push':
            if self.settings.github_mode != 'fake': raise ValueError('Fake mode required')
            with self.db.transaction() as conn:
                seq = conn.execute('SELECT coalesce(max(sequence),0)+1 FROM ui_fake_commits WHERE project_id=?', (pid,)).fetchone()[0]
                files = {'main.py': f'def evaluate():\n    return {seq}\n'}
                conn.execute('INSERT INTO ui_fake_commits(project_id,sequence,files_json) VALUES (?,?,?)', (pid, seq, json.dumps(files)))
            repo = ProjectStore(self.db, pid).project()['repo_full_name']
            # Rebuild from durable commits, including a previous interrupted publish.
            self.github.repos.pop(repo, None)
            self._fake_repo(pid)
            return {'head': self.github.head(repo, 'main').sha}
        if kind == 'review':
            project = ProjectStore(self.db, pid).project()
            if project['status'] != 'active' or not project['baseline_confirmed_at']: raise ValueError('Confirm the baseline and verify access first')
            with self.db.transaction(write=False) as conn:
                branch = conn.execute('SELECT tracked_branch FROM github_state WHERE project_id=?', (pid,)).fetchone()
            head = self.github.head(project['repo_full_name'], branch[0] if branch and branch[0] else self.github.repository(project['repo_full_name']).default_branch)
            cid = ProjectStore(self.db, pid).create_cycle(head.sha, trigger='manual', before_sha=project['last_reviewed_sha'])
            self.monitoring(pid, True, batch)
            return {'cycle_id': cid}
        if kind == 'resume':
            cid = self.engine.resume(pid, data['id'], batch_id=batch)
            self.monitoring(pid, True, batch)
            return {'resumed': cid}
        if kind == 'send':
            result = EmailService(self.db, self.settings, configured_sender(self.settings)).send(pid, data['id'])
            return {'draft_id': data['id'], 'status': result['status'], 'uncertain': result['delivery_uncertain']}
        if kind == 'smtp_check':
            import smtplib
            import ssl
            cfg = self.settings
            smtp = None
            try:
                if cfg.smtp_tls_mode == 'ssl':
                    smtp = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.request_timeout_seconds, context=ssl.create_default_context())
                else:
                    smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.request_timeout_seconds)
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if cfg.smtp_username: smtp.login(cfg.smtp_username, cfg.smtp_password.get_secret_value())
                return {'status': 'SMTP TLS/authentication verified; no email sent'}
            finally:
                if smtp: smtp.close()
        if kind in {'replies', 'google_check'}:
            from app.integrations.gmail.oauth import gmail_service
            from app.integrations.gmail.reader import GmailReader
            from app.scheduler.replies import ReplyPoller
            reader = GmailReader(gmail_service(self.settings, 'read'), self.settings)
            if kind == 'google_check':
                reader.list_page(query='in:inbox')
                return {'status': 'Gmail read connection verified'}
            return ReplyPoller(self.db, self.settings, reader).poll_project(pid)
        if kind == 'sheets':
            from app.integrations.sheets.oauth import sheets_service
            from app.integrations.sheets.client import SheetsClient
            from app.orchestrator.sheets import RegisterSync
            return RegisterSync(self.db, self.settings, SheetsClient(sheets_service(self.settings), self.settings)).sync()
        if kind == 'export':
            from tempfile import TemporaryDirectory
            from pathlib import Path
            from app.integrations.sheets.excel import export_excel
            with TemporaryDirectory(prefix='supervisorai-export-') as directory:
                path = export_excel(self.db, Path(directory) / 'progress.xlsx')
                return {'filename': 'progress.xlsx', '_download': path.read_bytes()}
        raise ValueError('Unknown action')

    def tick(self):
        with self._execution:
            with self.db.transaction() as conn:
                task = conn.execute("SELECT * FROM ui_tasks WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                if task:
                    conn.execute("UPDATE ui_tasks SET status='running' WHERE project_id=? AND id=?", (task['project_id'], task['id']))
            if task:
                try:
                    result, error = self._action(task), None
                except Exception:
                    result, error = None, 'action_failed_check_configuration_or_saved_state'
                output = result.pop('_download', None) if isinstance(result, dict) else None
                with self.db.transaction() as conn:
                    conn.execute("UPDATE ui_tasks SET status=?,result_json=?,output_blob=?,error=?,input_blob=NULL,finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND id=?", ('failed' if error else 'completed', json.dumps(result, default=str) if result is not None else None, output, error, task['project_id'], task['id']))
            with self.db.transaction(write=False) as conn:
                controls = conn.execute('SELECT * FROM ui_controls WHERE monitoring=1').fetchall()
                drafts = conn.execute("SELECT project_id,id FROM baseline_drafts WHERE status='extracting'").fetchall()
            for draft in drafts:
                try:
                    self.baselines.collect(draft['project_id'], draft['id'])
                except Exception:
                    # One malformed onboarding record must not starve other projects.
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE baseline_drafts SET status='pending',reason='collection_failed_check_saved_state' WHERE project_id=? AND id=? AND status='extracting'", (draft['project_id'], draft['id']))
            for control in controls:
                pid = control['project_id']
                try:
                    self._fake_repo(pid)
                    self.poller.poll_project(pid)
                    self.poller.queue.flush(pid)
                    with self.db.transaction(write=False) as conn:
                        cycle = conn.execute("SELECT id,status FROM cycles WHERE project_id=? AND trigger!='proposal_upload' AND status IN ('queued','running','pending_evidence_review') ORDER BY rowid LIMIT 1", (pid,)).fetchone()
                    if cycle and cycle['status'] != 'pending_evidence_review':
                        self.engine.step(pid, cycle['id'], batch_id=control['batch_id'])
                    if control['replies_enabled']:
                        if self._reply_poller is None:
                            from app.integrations.gmail.oauth import gmail_service
                            from app.integrations.gmail.reader import GmailReader
                            from app.scheduler.replies import ReplyPoller
                            self._reply_poller = ReplyPoller(self.db, self.settings, GmailReader(gmail_service(self.settings, 'read'), self.settings))
                        self._reply_poller.poll_project(pid)
                    error = None
                except Exception:
                    error = 'monitoring_deferred_check_access_or_saved_state'
                with self.db.transaction() as conn:
                    conn.execute('UPDATE ui_controls SET last_error=? WHERE project_id=?', (error, pid))

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception:
                self.last_error = 'worker_deferred_check_database_and_restart'
            self._stop.wait(1)


_instances = {}
_guard = threading.Lock()


def get_runtime(settings):
    with _guard:
        key = str(settings.database_path)
        runtime = _instances.get(key)
        if runtime is None or runtime.closed:
            runtime = _instances[key] = Runtime(settings)
            atexit.register(runtime.close)
        elif runtime.settings != settings:
            raise ValueError('Configuration changed. Restart Streamlit to apply it.')
        return runtime
