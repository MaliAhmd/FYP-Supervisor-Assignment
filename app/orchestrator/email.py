"""Supervisor approval and at-most-one automatic delivery attempt per approval."""
from email.message import EmailMessage
from email.utils import formatdate
import json
import re

from app.config import validate_email
from app.db.repository import new_id
from app.gateway.prompts import digest
from app.gateway.service import _WorkerLock
from app.integrations.smtp.sender import DeliveryError


def message_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'<[^<>\s@]+@[^<>\s@]+>', value):
        raise ValueError('Invalid Message-ID')
    return value


class EmailService:
    def __init__(self, db, settings, sender):
        self.db, self.settings, self.sender = db, settings, sender

    def get(self, pid, draft_id):
        with self.db.transaction(write=False) as conn:
            return dict(self._row(conn, pid, draft_id))

    def preview(self, pid, draft_id):
        row = self.get(pid, draft_id)
        return {'draft_id': draft_id, 'revision': row['revision'], 'subject': row['subject'],
                'body': row['body'], 'registered_student': row['student_email'],
                'delivery_recipient': self._checked(row), 'mode': self.settings.email_mode,
                'transport': self.settings.email_sender, 'from': self.settings.smtp_from}

    @staticmethod
    def _row(conn, pid, draft_id):
        row = conn.execute('SELECT d.*,p.student_email FROM email_drafts d JOIN projects p ON p.id=d.project_id WHERE d.project_id=? AND d.id=?', (pid, draft_id)).fetchone()
        if row is None:
            raise LookupError('Draft not found for project')
        return row

    def _checked(self, row):
        if row['to_addr'] != row['student_email']:
            raise ValueError('Recipient must be the registered student')
        validate_email(row['to_addr'])
        message_id(row['message_id'])
        if row['in_reply_to']:
            message_id(row['in_reply_to'])
        if not row['subject'].strip() or len(row['subject']) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in row['subject']):
            raise ValueError('Invalid subject')
        if not row['body'].strip() or len(row['body']) > self.settings.email_max_body_chars:
            raise ValueError('Invalid body')
        return self.settings.test_redirect_email if self.settings.email_mode == 'test' else row['to_addr']

    def _hash(self, row):
        cfg = self.settings
        return digest({'subject': row['subject'], 'body': row['body'], 'to': row['to_addr'],
                       'actual_to': self._checked(row), 'mode': cfg.email_mode, 'sender': cfg.email_sender,
                       'from': cfg.smtp_from, 'smtp_host': cfg.smtp_host, 'smtp_port': cfg.smtp_port,
                       'tls': cfg.smtp_tls_mode, 'username': cfg.smtp_username,
                       'message_id': row['message_id'], 'reply': row['in_reply_to']})

    @staticmethod
    def _editable(row, revision):
        if row['revision'] != revision:
            raise ValueError('Draft changed; reload before deciding')
        if row['status'] == 'sent' or row['send_started_at'] or row['delivery_uncertain']:
            raise ValueError('Delivery is locked; resolve the existing attempt first')

    @staticmethod
    def _decision(conn, pid, draft_id, decision, payload=None):
        conn.execute("INSERT INTO supervisor_decisions(id,project_id,kind,target_id,decision,actor,payload_json) VALUES (?,?,'email',?,?,'supervisor',?)", (new_id(), pid, draft_id, decision, json.dumps(payload or {})))
        conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'email.decision','supervisor',?)", (pid, json.dumps({'draft_id': draft_id, 'decision': decision})))

    def edit(self, pid, draft_id, *, expected_revision, subject, body):
        with self.db.transaction() as conn:
            row = dict(self._row(conn, pid, draft_id))
            self._editable(row, expected_revision)
            row.update(subject=subject, body=body)
            self._checked(row)
            conn.execute("UPDATE email_drafts SET subject=?,body=?,status='pending',approved_at=NULL,approval_hash=NULL,error=NULL,revision=revision+1 WHERE project_id=? AND id=?", (subject, body, pid, draft_id))
            self._decision(conn, pid, draft_id, 'edited')
        return self.get(pid, draft_id)

    def decide(self, pid, draft_id, *, expected_revision, approve):
        with self.db.transaction() as conn:
            row = self._row(conn, pid, draft_id)
            self._editable(row, expected_revision)
            fingerprint = self._hash(row) if approve else None
            conn.execute("""UPDATE email_drafts SET status=?,approval_hash=?,approved_at=CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
                revision=revision+1,error=NULL WHERE project_id=? AND id=?""", ('approved' if approve else 'rejected', fingerprint, approve, pid, draft_id))
            self._decision(conn, pid, draft_id, 'approved' if approve else 'rejected',
                           {'approval_hash': fingerprint, 'mode': self.settings.email_mode,
                            'delivery_recipient': self._checked(row) if approve else None})
        return self.get(pid, draft_id)

    def _lock(self, pid, draft_id):
        return _WorkerLock(self.db.path.with_suffix('.email-' + digest([pid, draft_id]) + '.lock'))

    def send(self, pid, draft_id):
        lock = self._lock(pid, draft_id)
        try:
            with self.db.transaction() as conn:
                row = self._row(conn, pid, draft_id)
                if row['status'] == 'sent':
                    return dict(row)
                if row['send_started_at'] or row['delivery_uncertain']:
                    raise ValueError('Existing delivery attempt requires resolution; do not resend')
                if row['status'] != 'approved' or not row['approved_at'] or row['approval_hash'] != self._hash(row):
                    raise ValueError('Current content and delivery settings require supervisor approval')
                recipient = self._checked(row)
                msg = EmailMessage()
                msg['From'], msg['To'] = self.settings.smtp_from, recipient
                msg['Subject'], msg['Message-ID'], msg['Date'] = row['subject'], row['message_id'], formatdate(localtime=False)
                if row['in_reply_to']:
                    msg['In-Reply-To'] = msg['References'] = row['in_reply_to']
                msg.set_content(row['body'])
                if len(msg.as_bytes()) > self.settings.email_max_bytes:
                    raise ValueError('Message exceeds configured size limit')
                attempt = new_id()
                conn.execute("INSERT INTO email_attempts(id,project_id,draft_id,actual_to,mode,transport,status) VALUES (?,?,?,?,?,?,'started')", (attempt, pid, draft_id, recipient, self.settings.email_mode, self.settings.email_sender))
                conn.execute("""UPDATE email_drafts SET send_started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),delivery_uncertain=1,
                    actual_to=?,delivery_mode=?,revision=revision+1 WHERE project_id=? AND id=?""", (recipient, self.settings.email_mode, pid, draft_id))
            try:
                receipt = self.sender.send(msg, recipient=recipient)
            except Exception as error:
                uncertain = error.uncertain if isinstance(error, DeliveryError) else True
                code = str(error) if isinstance(error, DeliveryError) else 'delivery_failed'
                with self.db.transaction() as conn:
                    conn.execute("UPDATE email_attempts SET status=?,error=?,finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND id=?", ('uncertain' if uncertain else 'failed', code, pid, attempt))
                    conn.execute("UPDATE email_drafts SET status='failed',error=?,delivery_uncertain=?,send_started_at=CASE WHEN ? THEN send_started_at ELSE NULL END,approval_hash=NULL,approved_at=NULL WHERE project_id=? AND id=?", (code, int(uncertain), uncertain, pid, draft_id))
            else:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE email_attempts SET status='sent',finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND id=?", (pid, attempt))
                    conn.execute("UPDATE email_drafts SET status='sent',sent_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),delivery_uncertain=0,error=NULL,provider_message_id=? WHERE project_id=? AND id=?", (receipt.provider_id, pid, draft_id))
            return self.get(pid, draft_id)
        finally:
            lock.close()

    def resolve(self, pid, draft_id, *, expected_revision, delivered, note):
        """Explicit supervisor reconciliation after checking the mailbox/provider."""
        if not note.strip():
            raise ValueError('A reconciliation note is required')
        lock = self._lock(pid, draft_id)
        try:
            with self.db.transaction() as conn:
                row = self._row(conn, pid, draft_id)
                if row['revision'] != expected_revision or not row['delivery_uncertain'] or not row['send_started_at']:
                    raise ValueError('Expected an unresolved delivery attempt')
                conn.execute("""UPDATE email_attempts SET status=?,finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE project_id=? AND draft_id=? AND status IN ('started','uncertain')""", ('confirmed_sent' if delivered else 'confirmed_not_sent', pid, draft_id))
                conn.execute("""UPDATE email_drafts SET status=?,delivery_uncertain=0,error=NULL,revision=revision+1,
                    send_started_at=CASE WHEN ? THEN send_started_at ELSE NULL END,
                    sent_at=CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
                    approved_at=CASE WHEN ? THEN coalesce(approved_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ELSE NULL END,
                    approval_hash=NULL WHERE project_id=? AND id=?""", ('sent' if delivered else 'pending', delivered, delivered, delivered, pid, draft_id))
                self._decision(conn, pid, draft_id, 'confirmed_sent' if delivered else 'confirmed_not_sent', {'note': note})
        finally:
            lock.close()
        return self.get(pid, draft_id)


def configured_sender(settings):
    from app.integrations.smtp.sender import SmtpSender
    if settings.email_sender == 'smtp':
        return SmtpSender(settings)
    from app.integrations.gmail.oauth import gmail_service
    from app.integrations.gmail.sender import GmailApiSender
    return GmailApiSender(gmail_service(settings, 'send'))
