import ast
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
import json
from pathlib import Path
import smtplib
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import ProjectStore
from app.gateway.types import GatewayError
from app.integrations.gmail import oauth
from app.integrations.gmail.reader import GmailReader, InboundMessage, MailReadError, decode_message
from app.integrations.gmail.sender import GmailApiSender
from app.integrations.smtp.sender import DeliveryError, Receipt, SmtpSender
from app.orchestrator.email import EmailService
from app.scheduler.replies import ReplyPoller


class Sender:
    def __init__(self, error=None):
        self.messages, self.error = [], error

    def send(self, message, *, recipient):
        self.messages.append((message, recipient))
        if self.error:
            raise self.error
        return Receipt('provider-123')


@pytest.fixture
def config():
    return Settings(_env_file=None, llm_mode='fake', github_mode='fake', email_mode='test', test_redirect_email='supervisor@example.com')


def draft(db, project, key='draft'):
    cid = project.create_cycle('cycle:' + key)
    with db.transaction() as conn:
        conn.execute('INSERT INTO email_drafts(id,project_id,cycle_id,to_addr,subject,body,message_id,idempotency_key) VALUES (?,?,?,?,?,?,?,?)',
                     (key, project.project_id, cid, project.project()['student_email'], 'Progress update', 'Please provide evidence.', '<' + key + '@supervisorai.local>', key))
    return key


def approved(service, pid, key):
    return service.decide(pid, key, expected_revision=service.get(pid, key)['revision'], approve=True)


def test_approval_required_redirect_idempotence_restart(db, project, config):
    pid, key, sender = project.project_id, draft(db, project), Sender()
    service = EmailService(db, config, sender)
    with pytest.raises(ValueError):
        service.send(pid, key)
    assert not sender.messages
    approved(service, pid, key)
    result = service.send(pid, key)
    assert result['status'] == 'sent' and result['delivery_uncertain'] == 0
    assert result['actual_to'] == config.test_redirect_email
    assert result['to_addr'] == project.project()['student_email']
    msg, address = sender.messages[0]
    assert msg['To'] == address == config.test_redirect_email
    assert msg['Message-ID'] == result['message_id']
    assert msg['Cc'] is None and msg['Bcc'] is None
    restarted = EmailService(db, config, sender)
    restarted.send(pid, key)
    assert len(sender.messages) == 1


def test_edit_invalidates_approval_and_optimistic_revision(db, project, config):
    key, pid = draft(db, project), project.project_id
    service = EmailService(db, config, Sender())
    row = approved(service, pid, key)
    with pytest.raises(ValueError):
        service.edit(pid, key, expected_revision=0, subject='stale', body='text')
    edited = service.edit(pid, key, expected_revision=row['revision'], subject='Updated', body='New feedback')
    assert edited['status'] == 'pending' and edited['approval_hash'] is None
    with pytest.raises(ValueError):
        service.send(pid, key)
    approved(service, pid, key)
    assert service.send(pid, key)['status'] == 'sent'


def test_reject_and_cross_project_access(db, project, config):
    key, pid = draft(db, project), project.project_id
    service = EmailService(db, config, Sender())
    service.decide(pid, key, expected_revision=0, approve=False)
    with pytest.raises(ValueError):
        service.send(pid, key)
    other = ProjectStore.create(db, 'Other', 'other@example.com', 'other/repo')
    with pytest.raises(LookupError):
        service.get(other.project_id, key)
    with pytest.raises(LookupError):
        service.decide(other.project_id, key, expected_revision=1, approve=True)


@pytest.mark.parametrize('field,value', [('to_addr', 'attacker@example.com'), ('subject', 'Injected\r\nBcc: attacker@example.com'), ('message_id', 'bad\nheader')])
def test_recipient_and_header_lock(db, project, config, field, value):
    key = draft(db, project)
    with db.transaction() as conn:
        conn.execute(f'UPDATE email_drafts SET {field}=? WHERE project_id=? AND id=?', (value, project.project_id, key))
    service = EmailService(db, config, Sender())
    with pytest.raises(ValueError):
        approved(service, project.project_id, key)


@pytest.mark.parametrize('updates', [{'email_mode': 'live'}, {'test_redirect_email': 'new@example.com'}, {'smtp_from': 'new@example.com'}, {'email_sender': 'gmail'}, {'smtp_host': 'otherhost'}])
def test_approval_bound_to_delivery_config(db, project, config, updates):
    key, pid, sender = draft(db, project), project.project_id, Sender()
    approved(EmailService(db, config, sender), pid, key)
    with pytest.raises(ValueError):
        EmailService(db, config.model_copy(update=updates), sender).send(pid, key)
    assert not sender.messages


def test_live_recipient_and_content_tamper(db, project, config):
    config = config.model_copy(update={'email_mode': 'live'})
    key, pid, sender = draft(db, project), project.project_id, Sender()
    service = EmailService(db, config, sender)
    approved(service, pid, key)
    with db.transaction() as conn:
        conn.execute("UPDATE email_drafts SET body='changed' WHERE project_id=? AND id=?", (pid, key))
    with pytest.raises(ValueError):
        service.send(pid, key)
    approved(service, pid, key)
    service.send(pid, key)
    assert sender.messages[0][1] == project.project()['student_email']


@pytest.mark.parametrize('delivered', [True, False])
def test_uncertain_delivery_requires_explicit_resolution(db, project, config, delivered):
    key, pid, sender = draft(db, project), project.project_id, Sender(DeliveryError())
    service = EmailService(db, config, sender)
    approved(service, pid, key)
    failed = service.send(pid, key)
    assert failed['status'] == 'failed' and failed['delivery_uncertain']
    with pytest.raises(ValueError):
        service.send(pid, key)
    with pytest.raises(ValueError):
        approved(service, pid, key)
    result = service.resolve(pid, key, expected_revision=failed['revision'], delivered=delivered, note='Checked provider mailbox and logs.')
    sender.error = None
    if delivered:
        assert result['status'] == 'sent'
        service.send(pid, key)
        assert len(sender.messages) == 1
    else:
        assert result['status'] == 'pending' and result['approved_at'] is None
        approved(service, pid, key)
        service.send(pid, key)
        assert len(sender.messages) == 2


def test_process_stop_after_reservation_never_auto_retries(db, project, config):
    key, pid, sender = draft(db, project), project.project_id, Sender(KeyboardInterrupt())
    service = EmailService(db, config, sender)
    approved(service, pid, key)
    with pytest.raises(KeyboardInterrupt):
        service.send(pid, key)
    fresh = EmailService(db, config, Sender())
    assert fresh.get(pid, key)['delivery_uncertain'] == 1
    with pytest.raises(ValueError):
        fresh.send(pid, key)
    with db.transaction(write=False) as conn:
        assert conn.execute('SELECT status FROM email_attempts WHERE project_id=?', (pid,)).fetchone()[0] == 'started'


def test_known_rejection_needs_new_approval(db, project, config):
    key, pid, sender = draft(db, project), project.project_id, Sender(DeliveryError('smtp_rejected', uncertain=False))
    service = EmailService(db, config, sender)
    approved(service, pid, key)
    row = service.send(pid, key)
    assert not row['delivery_uncertain'] and row['send_started_at'] is None
    with pytest.raises(ValueError):
        service.send(pid, key)
    sender.error = None
    approved(service, pid, key)
    assert service.send(pid, key)['status'] == 'sent'


def test_concurrent_sends_do_not_duplicate(db, project, config):
    key, pid, sender = draft(db, project), project.project_id, Sender()
    service = EmailService(db, config, sender)
    approved(service, pid, key)
    def send():
        try:
            return service.send(pid, key)['status']
        except GatewayError:
            return 'busy'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: send(), range(4)))
    assert 'sent' in results and len(sender.messages) == 1


class SMTPFake:
    def __init__(self, error=None, phase='send', close_error=False):
        self.calls, self.error, self.phase, self.close_error = [], error, phase, close_error

    def ehlo(self): self.calls.append('ehlo')
    def starttls(self, **kwargs):
        self.calls.append('tls')
        if self.phase == 'tls' and self.error: raise self.error
    def login(self, user, password): self.calls.append('login')
    def send_message(self, message, **kwargs):
        self.calls.append(('send', kwargs))
        if self.phase == 'send' and self.error: raise self.error
        return {}
    def close(self):
        self.calls.append('close')
        if self.close_error: raise OSError('close')


def wire_message():
    message = EmailMessage()
    message['To'], message['From'], message['Message-ID'] = 'supervisor@example.com', 'sender@example.com', '<test@example.com>'
    message['Subject'] = 'Test'
    message.set_content('Hello')
    return message


def test_smtp_tls_envelope_and_close_failure(config):
    fake = SMTPFake(close_error=True)
    SmtpSender(config, smtp_factory=lambda *a, **kw: fake).send(wire_message(), recipient='supervisor@example.com')
    assert fake.calls[:3] == ['ehlo', 'tls', 'ehlo']
    assert fake.calls[3][1]['to_addrs'] == ['supervisor@example.com']


@pytest.mark.parametrize('error,phase,uncertain', [(OSError('secret'), 'tls', False), (smtplib.SMTPDataError(550, b'secret'), 'send', False), (smtplib.SMTPServerDisconnected('secret'), 'send', True)])
def test_smtp_failure_classification_sanitized(config, error, phase, uncertain):
    fake = SMTPFake(error, phase)
    with pytest.raises(DeliveryError) as caught:
        SmtpSender(config, smtp_factory=lambda *a, **kw: fake).send(wire_message(), recipient='supervisor@example.com')
    assert caught.value.uncertain is uncertain and 'secret' not in str(caught.value)


class API:
    def __init__(self, results):
        self.results, self.calls = list(results), []
    def users(self): return self
    def messages(self): return self
    def send(self, **kwargs): self.calls.append(('send', kwargs)); return self
    def list(self, **kwargs): self.calls.append(('list', kwargs)); return self
    def get(self, **kwargs): self.calls.append(('get', kwargs)); return self
    def execute(self, **kwargs):
        assert kwargs == {'num_retries': 0}
        value = self.results.pop(0)
        if isinstance(value, BaseException): raise value
        return value


def test_gmail_sender_raw_mime_and_no_retry():
    api = API([{'id': 'sent-id'}])
    receipt = GmailApiSender(api).send(wire_message(), recipient='supervisor@example.com')
    assert receipt.provider_id == 'sent-id'
    raw = api.calls[0][1]['body']['raw']
    assert b'Message-ID: <test@example.com>' in base64.urlsafe_b64decode(raw)
    assert api.calls[0][1]['userId'] == 'me'


@pytest.mark.parametrize('status,uncertain', [(403, False), (429, False), (503, True), (None, True)])
def test_gmail_sender_failure(status, uncertain):
    error = RuntimeError('provider-secret')
    error.resp = SimpleNamespace(status=status)
    with pytest.raises(DeliveryError) as caught:
        GmailApiSender(API([error])).send(wire_message(), recipient='supervisor@example.com')
    assert caught.value.uncertain is uncertain and 'secret' not in str(caught.value)


def raw_reply(*, html=False, sender='student@example.com', refs='<draft@supervisorai.local>'):
    msg = EmailMessage()
    msg['From'], msg['To'] = sender, 'supervisor@example.com'
    msg['Message-ID'], msg['References'] = '<reply@example.com>', refs
    if html:
        msg.set_content('<p>Progress &amp; evidence</p><script>bad()</script>', subtype='html')
    else:
        msg.set_content('Progress evidence')
        msg.add_alternative('<p>duplicate HTML</p>', subtype='html')
        msg.add_attachment(b'ignored attachment', maintype='application', subtype='pdf', filename='report.pdf')
    return {'id': 'g1', 'threadId': 'thread', 'internalDate': '1793000000000', 'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode()}


@pytest.mark.parametrize('html', [False, True])
def test_mime_plain_preferred_html_sanitized(config, html):
    result = decode_message(raw_reply(html=html), config)
    assert result.gmail_id == 'g1' and result.references == ('<draft@supervisorai.local>',)
    assert 'Progress' in result.body and 'bad()' not in result.body
    assert 'duplicate' not in result.body and 'attachment' not in result.body


def test_reader_only_list_get_and_size_limits(config):
    record = raw_reply()
    api = API([{'messages': [{'id': 'g1'}], 'nextPageToken': 'next'}, record])
    reader = GmailReader(api, config)
    assert reader.list_page(query='from:student@example.com') == (['g1'], 'next')
    assert reader.read('g1').gmail_id == 'g1'
    assert [c[0] for c in api.calls] == ['list', 'get']
    assert api.calls[-1][1]['format'] == 'raw'
    with pytest.raises(MailReadError):
        decode_message(record, config.model_copy(update={'email_max_bytes': 10}))
    import app.integrations.gmail.reader as module
    tree = ast.parse(Path(module.__file__).read_text())
    assert not {'send', 'modify', 'delete', 'trash', 'batchModify'} & {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


def sent_draft(db, project, config, key='draft'):
    key = draft(db, project, key)
    service = EmailService(db, config, Sender())
    approved(service, project.project_id, key)
    return service.send(project.project_id, key)


def incoming(sender='supervisor@example.com', key='g1', refs=('<draft@supervisorai.local>',)):
    return InboundMessage(key, sender, '<reply@example.com>', None, refs, '2026-10-26T00:00:00Z', 'I finished the milestone.', 'thread')


def test_reply_match_sender_references_and_atomic_dedupe(db, project, config):
    sent_draft(db, project, config)
    poller = ReplyPoller(db, config, None)
    pid = project.project_id
    assert not poller.ingest(pid, incoming(sender='attacker@example.com'))
    assert not poller.ingest(pid, incoming(refs=('<unknown@example.com>',)))
    assert poller.ingest(pid, incoming())
    assert not poller.ingest(pid, incoming())
    with db.transaction(write=False) as conn:
        assert conn.execute('SELECT count(*) FROM email_inbound WHERE project_id=?', (pid,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM cycles WHERE project_id=? AND trigger='email_reply'", (pid,)).fetchone()[0] == 1


def test_reply_cross_project_ambiguous_references_rejected(db, project, config):
    sent_draft(db, project, config)
    other = ProjectStore.create(db, 'Other', 'other@example.com', 'other/repo')
    sent_draft(db, other, config, 'other-draft')
    poller = ReplyPoller(db, config, None)
    assert not poller.ingest(other.project_id, incoming())
    assert not poller.ingest(project.project_id, incoming(refs=('<draft@supervisorai.local>', '<other-draft@supervisorai.local>')))


class ReaderFake:
    def __init__(self): self.pages, self.reads = [], []
    def list_page(self, *, query, page_token):
        self.pages.append((query, page_token))
        return (['g1'], 'next') if page_token is None else (['g2'], None)
    def read(self, key):
        self.reads.append(key)
        return incoming(key=key)


def test_pagination_checkpoint_restart_and_poll_cooldown(db, project, config):
    sent_draft(db, project, config)
    config = config.model_copy(update={'gmail_pages_per_tick': 1})
    reader = ReaderFake()
    now = [datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()]
    poller = ReplyPoller(db, config, reader, clock=lambda: now[0])
    assert poller.poll_project(project.project_id)['status'] == 'more_pages'
    assert poller.poll_project(project.project_id)['status'] == 'waiting'
    now[0] += 301
    restarted = ReplyPoller(db, config, reader, clock=lambda: now[0])
    assert restarted.poll_project(project.project_id)['status'] == 'complete'
    assert reader.pages[1][1] == 'next' and reader.pages[0][0] == reader.pages[1][0]
    assert reader.reads == ['g1', 'g2']


def test_read_failure_does_not_advance_page(db, project, config):
    sent_draft(db, project, config)
    reader = ReaderFake()
    def fail(key): raise MailReadError()
    reader.read = fail
    poller = ReplyPoller(db, config, reader, clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
    assert poller.poll_project(project.project_id)['status'] == 'deferred'
    with db.transaction(write=False) as conn:
        row = conn.execute('SELECT * FROM gmail_poll_state WHERE project_id=?', (project.project_id,)).fetchone()
        assert row['page_token'] is None and row['watermark'] == 0 and row['failures'] == 1


def test_private_token_save_atomic_and_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / 'tokens' / 'fake.json'
    calls = []
    monkeypatch.setattr(oauth, 'protect', lambda p: calls.append(Path(p)))
    oauth.save_token(path, '{"fixture":true}')
    assert json.loads(path.read_text()) == {'fixture': True}
    assert calls[0] == path.parent and calls[1].name.startswith('.oauth-')
    def fail(path): raise oauth.OAuthError('blocked')
    monkeypatch.setattr(oauth, 'protect', fail)
    with pytest.raises(oauth.OAuthError):
        oauth.save_token(path, 'replacement')
    assert json.loads(path.read_text()) == {'fixture': True}


def test_oauth_separate_scopes_and_no_implicit_authorization(tmp_path, config, monkeypatch):
    assert set(oauth.SCOPES['read']).isdisjoint(oauth.SCOPES['send'])
    config = config.model_copy(update={'token_dir': tmp_path / 'tokens'})
    with pytest.raises(oauth.OAuthError, match='authorization_required'):
        oauth.credentials(config, 'read')
    config.token_dir.mkdir()
    (config.token_dir / 'gmail-read.json').write_text(json.dumps({'scopes': oauth.SCOPES['send']}))
    monkeypatch.setattr(oauth, 'protect', lambda p: None)
    with pytest.raises(oauth.OAuthError, match='scope_mismatch'):
        oauth.credentials(config, 'read')


def test_provider_acceptance_database_failure_stays_uncertain(db, project, config):
    import sqlite3
    key, pid, sender = draft(db, project), project.project_id, Sender()
    service = EmailService(db, config, sender)
    approved(service, pid, key)
    with db.transaction() as conn:
        conn.execute("CREATE TRIGGER simulate_commit_failure BEFORE UPDATE ON email_drafts WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT,'test'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        service.send(pid, key)
    assert service.get(pid, key)['delivery_uncertain'] == 1
    with pytest.raises(ValueError):
        EmailService(db, config, sender).send(pid, key)
    assert len(sender.messages) == 1


def test_reply_insert_and_queue_are_atomic(db, project, config):
    import sqlite3
    sent_draft(db, project, config)
    with db.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_reply_cycle BEFORE INSERT ON cycles WHEN NEW.trigger='email_reply' BEGIN SELECT RAISE(ABORT,'test'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        ReplyPoller(db, config, None).ingest(project.project_id, incoming())
    with db.transaction(write=False) as conn:
        assert conn.execute('SELECT count(*) FROM email_inbound WHERE project_id=?', (project.project_id,)).fetchone()[0] == 0


def test_preview_shows_test_destination(db, project, config):
    key = draft(db, project)
    preview = EmailService(db, config, Sender()).preview(project.project_id, key)
    assert preview['registered_student'] == project.project()['student_email']
    assert preview['delivery_recipient'] == config.test_redirect_email
    assert preview['mode'] == 'test'


def test_smtp_ssl_selected_and_auth(config):
    fake = SMTPFake()
    config = config.model_copy(update={'smtp_tls_mode': 'ssl', 'smtp_username': 'user@example.com'})
    def plain(*args, **kwargs): raise AssertionError('Must use implicit TLS')
    SmtpSender(config, smtp_factory=plain, ssl_factory=lambda *a, **kw: fake).send(wire_message(), recipient='supervisor@example.com')
    assert fake.calls[0] == 'login' and 'tls' not in fake.calls


def test_reply_sender_for_live_delivery_uses_registered_student(db, project, config):
    sent_draft(db, project, config.model_copy(update={'email_mode': 'live'}))
    poller = ReplyPoller(db, config, None)
    assert not poller.ingest(project.project_id, incoming())
    assert poller.ingest(project.project_id, incoming(sender=project.project()['student_email']))


def test_in_reply_to_alone_matches(db, project, config):
    sent_draft(db, project, config)
    reply = InboundMessage('reply2', config.test_redirect_email, '<reply2@example.com>', '<draft@supervisorai.local>', (), '2026-10-26T00:00:00Z', 'Progress')
    assert ReplyPoller(db, config, None).ingest(project.project_id, reply)
