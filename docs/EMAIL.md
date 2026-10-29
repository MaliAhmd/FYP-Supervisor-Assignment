# Phase 6: approval, delivery and replies

The backend supports SMTP or Gmail API delivery, revision-checked approval/edit/
rejection, durable attempts and a read-only Gmail reply poller. The Streamlit
approval controls are available in the UI. No live account is contacted by imports,
migrations, tests or the offline demo.

```powershell
.\.venv\Scripts\python.exe -m scripts.email_demo
```

This creates a temporary fictional draft, explicitly approves it, simulates one
redirected delivery, restarts the service and verifies no resend. It imports a
matching fictional reply exactly once and queues a review. It ignores the `.env`
file and uses an in-memory recording sender, not SMTP or Google.

## Approval API

```python
from app.orchestrator.email import EmailService, configured_sender

service = EmailService(db, settings, configured_sender(settings))
preview = service.preview(project_id, draft_id)
# Present subject, body, registered student, actual delivery recipient, mode,
# sender and transport to the supervisor. Use the displayed revision below.
service.decide(project_id, draft_id,
               expected_revision=preview['revision'], approve=True)
# Only after that explicit approval, application code may deliver:
result = service.send(project_id, draft_id)
```

`edit(..., expected_revision=..., subject=..., body=...)` returns the updated draft
and clears approval. `decide(..., approve=False)` rejects it. Neither API accepts
a recipient. Editing a sent/in-flight/uncertain draft is forbidden. Stale revisions
are rejected. Previewing does not approve anything. The application owns this
supervisor API; it is not an agent tool or an authentication boundary for remote
users. The local single-supervisor UI exposes these controls without remote authentication.

Approval hashes bind the message text, registered recipient, actual destination,
mode, configured sender/transport and SMTP routing settings. Changing these
requires fresh approval. A recipient differing from the project's registered
student is rejected, including in test mode. Fresh MIME messages have exactly one
To header and no Cc/Bcc. Subject/header injection is rejected. Existing generated
draft Message-IDs remain stable across attempts; draft idempotency is enforced by
the database's unique `(project_id, cycle_id, kind)` key.

**`EMAIL_MODE=test` redirects real delivery; it is not a dry run.** The SMTP/Gmail
adapter still delivers, but only to `TEST_REDIRECT_EMAIL`. Use the offline demo
or an injected recording sender for zero network activity. Test replies must come
from the address to which that particular outgoing message was redirected.

## Delivery and uncertain results

An OS lock serializes delivery/reconciliation for a draft. Before calling a
provider, one transaction records the attempt, actual destination/mode and a
durable uncertain flag. A successful provider acknowledgement records sent state;
repeating `send` then returns that state without another provider call.

Failures before SMTP submission, explicit SMTP rejection, and selected explicit
Gmail 4xx rejections are known failures. They clear approval and need fresh
supervisor approval before retrying. A disconnect during SMTP submission, ambiguous
Gmail failure, process termination, or a database failure after provider acceptance
keeps the attempt locked. There are no automatic send retries. Gmail `.execute`
uses `num_retries=0`; the authentication library may refresh an expired credential
after a 401. Cleanup errors after successful SMTP acceptance do not change success.

Check the sent mailbox/provider logs using the saved Message-ID, then explicitly
record the result:

```python
row = service.get(project_id, draft_id)
service.resolve(project_id, draft_id, expected_revision=row['revision'],
                delivered=True, note='Verified this Message-ID in Sent mail.')
# If verified not delivered, delivered=False returns it to pending, unapproved.
```

An inconclusive check must remain unresolved. SMTP and Gmail do not provide an
application-controlled exactly-once transaction with SQLite; a Message-ID is a
correlation identifier, not a provider deduplication guarantee. Incorrectly
declaring an uncertain attempt not delivered can cause a duplicate on manual retry.
All decisions and attempts remain recorded. Sent means provider acceptance, not
proof of inbox delivery or that the student read the message.

## SMTP setup

For a Gmail account that permits app passwords, enable 2-Step Verification and
create an app password. Put it only in the ignored `.env`, with the full Gmail
address as `SMTP_USERNAME` and an authorized `SMTP_FROM`. Gmail documents
`smtp.gmail.com` with TLS/STARTTLS on port 587 and app-password use for suitable
accounts. Work/school restrictions may require administrator guidance.
[Google's mail-client setup](https://support.google.com/mail/answer/7104828?hl=en-en)

```dotenv
EMAIL_MODE=test
EMAIL_SENDER=smtp
TEST_REDIRECT_EMAIL=your-supervisor-mailbox@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS_MODE=starttls
SMTP_USERNAME=your-account@gmail.com
SMTP_PASSWORD=
SMTP_FROM=your-account@gmail.com
```

Fill the password locally. Other SMTP hosts are supported. Implicit TLS uses
`SMTP_TLS_MODE=ssl` and the host's TLS port (commonly 465). Certificate verification
is enabled; STARTTLS cannot silently fall back to plaintext. Username/password
authentication happens only after TLS. No SMTP XOAUTH2 adapter is implemented;
use the optional Gmail API sender when app passwords are unavailable.

## Google Cloud desktop OAuth

1. Create/select a Google Cloud project and enable the Gmail API.
2. Configure the consent screen for your application; for external testing, add
   the supervisor's Google account as a test user.
3. Create a **Desktop app** OAuth client and download its JSON outside the source
   tree. Set `GOOGLE_CLIENT_SECRETS_FILE` to that path.
4. Keep `TOKEN_DIR` outside the source tree (default `~/.supervisorai/tokens`).
5. Explicitly authorize the read-only account:

```powershell
.\.venv\Scripts\python.exe -m scripts.authorize_gmail read
```

This opens the system browser and uses a loopback callback; it is never invoked
implicitly by a background worker. The reader requests only
`https://www.googleapis.com/auth/gmail.readonly`. Optional API delivery is separate:

```powershell
.\.venv\Scripts\python.exe -m scripts.authorize_gmail send
```

Then set `EMAIL_SENDER=gmail`, with `SMTP_FROM` set to the account's authorized
sending address. Only `gmail.send` is requested for that token. Separate
`gmail-read.json` and `gmail-send.json` files prevent loading send credentials into
the reader; credentials with unexpected scope sets are refused. SMTP needs only
the read authorization for reply ingestion. Google client libraries implement the
desktop flow and refresh. The API sender submits base64url RFC MIME with our saved
Message-ID. It does not promise Gmail UI thread placement for outbound follow-ups;
inbound correlation uses Message-ID references, not Gmail thread IDs.
[Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python),
[message resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)

Token files are atomically replaced inside an owner-private directory. POSIX uses
directory mode 0700 and file mode 0600. Windows replaces the DACL with a protected
current-user-only rule. Permission failures abort saving; they never fall back to
unprotected token writes. Existing files are protected before reading. Refreshes
at service startup are persisted; library refreshes during subsequent requests may
remain in memory until the next startup refresh. Keep the token directory dedicated
to this application's credentials.

External Google OAuth projects in Testing may receive refresh tokens expiring
after seven days for Gmail scopes, requiring reauthorization. No real account
authorization or provider delivery was performed during this phase.
[Google OAuth documentation](https://developers.google.com/identity/protocols/oauth2/)

## Reply worker and matching

```python
from app.integrations.gmail.oauth import gmail_service
from app.integrations.gmail.reader import GmailReader
from app.scheduler.replies import ReplyPoller

reader = GmailReader(gmail_service(settings, 'read'), settings)
poller = ReplyPoller(db, settings, reader)
worker = poller.background()
worker.start()
# Shut down when the application stops:
worker.close(timeout=120)
```

The reader exposes only message `list` and `get`, with no mark-read, label, delete,
draft or send operations. Queries use sender/date filters; messages are fetched
in raw MIME format. The default poll interval is 300 seconds. A fixed query/time
window, page token and watermark are persisted per project. Default three pages
of up to 100 messages per tick bound work. Failed reads do not advance that page;
retries use persisted exponential backoff. Restart resumes pagination. Completed
scans overlap the previous day and deduplicate Gmail IDs. This supports normal
mail arrival, not arbitrary backdated mailbox imports older than the overlap.
[List API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list),
[Get API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)

`In-Reply-To` or `References` must match stored sent/uncertain outgoing Message-IDs.
References spanning multiple projects are rejected as ambiguous, including when
several projects share a test redirect mailbox. A single From address must match
the registered student for live delivery or the stored redirect for test delivery.
Subjects and Gmail thread IDs alone never select a project. Matching header values
does not establish cryptographic sender identity; reply content remains untrusted
and cannot approve deadlines, complete milestones or send email automatically.

MIME parsing prefers plain text, falls back to HTML text with script/style content
discarded, and excludes attachments/duplicate alternatives. Default raw-message
limit is 1 MiB, with at most 100,000 body characters. Oversized, malformed or no-text
messages are skipped with a metadata-only audit record. Attachments are not imported
as reports; upload report PDFs through PDFStore. Matching replies and their
`email_reply` cycles commit together. Existing review workers consume the persisted
body as evidence and mark it processed only when review completes. Repeated polls
do not create duplicate replies or review cycles. Paused projects are not polled.
