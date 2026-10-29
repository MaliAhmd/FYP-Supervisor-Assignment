"""Offline approval/send/reply/restart demo. No SMTP connection or Gmail account."""
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.db import Database, ProjectStore
from app.integrations.gmail.reader import InboundMessage
from app.integrations.smtp.sender import Receipt
from app.orchestrator.email import EmailService
from app.scheduler.replies import ReplyPoller


class RecordingSender:
    def __init__(self):
        self.calls = []

    def send(self, message, *, recipient):
        self.calls.append((message, recipient))
        return Receipt('fake-receipt')


def main():
    with TemporaryDirectory(prefix='supervisorai-email-') as directory:
        db = Database(Path(directory) / 'demo.sqlite3')
        db.migrate()
        settings = Settings(_env_file=None, llm_mode='fake', github_mode='fake', email_mode='test', test_redirect_email='supervisor@example.com')
        project = ProjectStore.create(db, 'Fictional Student', 'student@example.com', 'fictional/email-demo')
        pid = project.project_id
        cid = project.create_cycle('demo-draft')
        with db.transaction() as conn:
            conn.execute("UPDATE cycles SET status='completed' WHERE project_id=? AND id=?", (pid, cid))
            conn.execute("""INSERT INTO email_drafts(id,project_id,cycle_id,to_addr,subject,body,message_id,idempotency_key)
                VALUES ('draft',?,?,'student@example.com','Progress evidence','Please provide evaluation results.','<demo@supervisorai.local>','demo-draft')""", (pid, cid))
        sender = RecordingSender()
        service = EmailService(db, settings, sender)
        preview = service.preview(pid, 'draft')
        assert preview['delivery_recipient'] == 'supervisor@example.com'
        service.decide(pid, 'draft', expected_revision=preview['revision'], approve=True)
        assert service.send(pid, 'draft')['status'] == 'sent'
        restarted = EmailService(Database(db.path), settings, sender)
        restarted.send(pid, 'draft')
        assert len(sender.calls) == 1
        reply = InboundMessage('fake-reply', 'supervisor@example.com', '<reply@example.com>', '<demo@supervisorai.local>', (),
                               '2026-10-26T00:00:00Z', 'Evaluation is complete; please review the evidence.')
        poller = ReplyPoller(db, settings, None)
        assert poller.ingest(pid, reply)
        assert not ReplyPoller(Database(db.path), settings, None).ingest(pid, reply)
        with db.transaction(write=False) as conn:
            assert conn.execute("SELECT count(*) FROM cycles WHERE project_id=? AND trigger='email_reply'", (pid,)).fetchone()[0] == 1
        print('Approved fixture draft; fake delivery redirected to the test mailbox.')
        print('Restart did not repeat delivery. Matching reply imported once and queued one review.')
        print('No real email, OAuth authorization, or live API call occurred.')


if __name__ == '__main__':
    main()
