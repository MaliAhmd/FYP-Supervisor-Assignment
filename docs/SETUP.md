# Setup, run and test SupervisorAI

Run PowerShell commands from `F:\Assignment Agentic 2` (or your checkout root).
Python 3.11+ is required. The validated environment uses Python 3.11.2.

## 1. Install and configure

For a new checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

An existing virtual environment can be reused. Do not overwrite your populated
`.env`. Keep credentials there; `.env.example` is a shareable blank template.
Environment variables override `.env`. On macOS/Linux use `.venv/bin/python`.
Use `SMTP_TLS_MODE=starttls` or `SMTP_TLS_MODE=ssl`; `SMTP_SECURE` is not a
recognized setting and is ignored. Unknown settings do not configure services.

For the first local UI test, use `LLM_MODE=fake`, `GITHUB_MODE=fake` and
`EMAIL_MODE=test`. `SMTP_FROM` and `TEST_REDIRECT_EMAIL` must each be one bare
email address without a display name, angle brackets or an `SMTP_FROM:` prefix.
Use your own mailbox as the test redirect before sending anything.

```powershell
.\.venv\Scripts\python.exe -m scripts.check_env
```

This performs local validation and checks Google file presence/JSON shape without
network access, refreshing tokens, or displaying settings values. Exit code 1
means invalid settings; fix those fields and rerun to see optional integration
readiness. Exit code 0 means settings parse, not that all integrations work.
Google authorization is optional for offline reviews and local Excel export.

## 2. Start the application

```powershell
.\.venv\Scripts\python.exe -m app init-db
.\.venv\Scripts\python.exe -m app status
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Open `http://127.0.0.1:8501`. Keep the terminal running. Use `Ctrl+C` to stop,
then run the Streamlit command again to resume saved work. Restart after changing
configuration. Run one app process per database; do not run standalone pollers
alongside the UI. The UI owns the background workers.

The database defaults to `data/supervisorai.sqlite3`; initialization is repeatable
and retains existing data. Do not delete it to resolve a configuration error.
Stop the app before making a filesystem backup, and retain the database with any
remaining `-wal`/`-shm` sidecars together. Source code and `.env` are separate from
the database; keep credential backups private. This app is for local single-user
hosting and has no remote-user authentication.

## 3. Connect live services when needed

### Groq

Create a key in the Groq console and place it in `GROQ_API_KEY`. Set
`GROQ_MODEL_FAST` and `GROQ_MODEL_SMART` to available model IDs supporting the
gateway's JSON chat-completion requests, then set `LLM_MODE=live`. Use the
provider's current account/model limits to configure local pacing; the project
does not hardcode model names or guarantee free-tier availability.
[Groq quickstart](https://console.groq.com/docs/quickstart),
[models](https://console.groq.com/docs/models),
[gateway behavior](GATEWAY.md).

### GitHub

Set `GITHUB_MODE=live` and `GITHUB_TOKEN` to a supervisor user token able to read
the intended repositories. In Projects, confirm the baseline, discover/accept the
student's invitation and verify access. A read-capable token can also verify
access after you accept the invitation directly on GitHub. Invitation acceptance
has different permissions from reading contents; see [the integration guide](GITHUB.md).

If using an OAuth app, register it under **Settings > Developer settings > OAuth
apps > New OAuth App**, supplying its name, homepage and the callback URL handled
by your authorization helper. Complete GitHub's documented authorization-code or
device flow externally, then store the resulting user token as `GITHUB_TOKEN`.
SupervisorAI has no GitHub OAuth callback route or token-exchange/refresh helper;
entering `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` alone will not connect it.
An expiring token needs external renewal and a configuration restart. Do not use
the Streamlit address as a pretend OAuth callback.
[Register an OAuth app](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app),
[authorization flows](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

### SMTP delivery

For Gmail SMTP, set the following with your actual account details:

```dotenv
EMAIL_SENDER=smtp
EMAIL_MODE=test
TEST_REDIRECT_EMAIL=your-own-mailbox@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS_MODE=starttls
SMTP_USERNAME=your-account@gmail.com
SMTP_FROM=your-account@gmail.com
SMTP_PASSWORD=
```

Fill `SMTP_PASSWORD` locally with an app password if your account supports it.
Google requires 2-Step Verification for app passwords; managed accounts may
restrict their availability. [Google app-password help](https://support.google.com/mail/answer/185833).
Other SMTP hosts and implicit TLS are supported; see [EMAIL.md](EMAIL.md).
**Test mode sends real mail to the redirect address.** Fake LLM/GitHub modes do
not fake SMTP. The Setup SMTP check authenticates without submitting a message.

### Google: replies and Sheets

In a Google Cloud project, enable **Gmail API** and **Google Sheets API**.
Configure OAuth consent in Testing with your supervisor account as a test user,
then create a **Desktop app** OAuth client and download its JSON. Set
`GOOGLE_CLIENT_SECRETS_FILE` to that actual file, preferably outside the checkout;
keep `TOKEN_DIR` outside the source tree. A client ID or a nonexistent path is not
a substitute for the downloaded JSON.
[Google's Python setup guide](https://developers.google.com/workspace/gmail/api/quickstart/python).

Authorize only the features you will use; these commands open a browser:

```powershell
.\.venv\Scripts\python.exe -m scripts.authorize_gmail read
.\.venv\Scripts\python.exe -m scripts.authorize_sheets
```

Read authorization is for replies, including when SMTP sends outgoing mail.
For optional Gmail API delivery, also run `scripts.authorize_gmail send` and set
`EMAIL_SENDER=gmail`. It uses a separate send token. No send authorization is
needed for SMTP.

For Sheets, create a spreadsheet and a `Progress` tab with at least 12 columns and
enough rows. Set `GOOGLE_SHEET_ID` to the ID in its URL, not the whole URL, and
`GOOGLE_SHEET_TAB=Progress`. Generated data occupies A:L; put manual notes in M
onward. Sync is explicit from Register. Google token presence is not proof of
valid authorization; use Setup's read check and the relevant explicit action.
See [EMAIL.md](EMAIL.md) and [SHEETS.md](SHEETS.md) for scopes and conflict handling.

## Test from the terminal

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q app scripts tests streamlit_app.py
$demoPath = "data/demo-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff')"
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory $demoPath
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory $demoPath --resume
```

Tests use temporary databases, fake providers and mocked HTTP/SMTP, not your live
account credentials. Keep the example file valid and free of secrets. The full
demo ignores `.env` and environment integration settings. It uses a fixed
2026-10-26 scenario date, regardless of today's date.

Expected demo: 3 projects, 6 milestones, 9 completed reviews plus 3 onboarding
cycles, 9 drafts, 2 report versions, 1 unsupported claim and retained superseded
deadline history. A real subprocess resumes after round two. `summary.json`
reports the checks, and `progress.xlsx` is the export. Completed `--resume` must
not add work. Always use a fresh destination for a new run.

For manual stop/restart, use a fresh path:

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory data/manual-restart --stop-after-2
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory data/manual-restart --resume
```

The scripted demo's fake remote history lives in its manifest. Use a separate
normal UI database for interactive testing; the UI does not import that manifest
into its own fake commit history. Detailed scenarios: [DEMO.md](DEMO.md).

## Test in the browser

1. Keep fake LLM/GitHub mode for the first pass. In **Projects**, add a fictional
   student/repository, save a proposal PDF and request extraction. Fake extraction
   deliberately produces no invented milestones: enter two milestones, criteria
   and ISO dates yourself. Save, acknowledge coverage and confirm the baseline.
2. Create a sample code commit, verify fake repository access, then use **Review
   now**. Watch stage progress and refresh saved state when complete. Repeating
   Review now for the same SHA should not create a second review or draft.
3. In **Approvals**, edit and approve a draft. Approval alone must not send it.
   Leave Send untouched for an offline test. In **Register**, export Excel.
4. Stop Streamlit and restart. Verify the saved baseline, review, draft and
   evidence remain. Create another sample commit and review the new evidence.
5. For live testing, switch to a separate test database and real test repository,
   configure live models/token and keep `EMAIL_MODE=test` with your own redirect.
   Follow [the real-repository scenario](DEMO.md). Use Setup checks first. After
   inspecting the displayed recipient/text, explicitly approve and send one test
   draft. Reply from the redirect mailbox, enable Gmail reply polling, and verify
   the reply is linked once. Exercise a deadline proposal/approval and inspect
   superseded history. Sync a dedicated Sheet twice and verify a note in M is
   retained. These live actions contact providers and require working accounts.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Configuration page instead of dashboard | Run `scripts.check_env`; fix reported fields and restart. |
| Blank baseline in fake mode | Enter objectives/milestones manually; the seeded demo has scripted extraction fixtures. |
| Queued report/reply cycle stays idle | Enable monitoring for the project; refresh saved state. |
| New push waits | Polling defaults to 60 seconds with a 300-second debounce; inspect project access and prior unfinished cycle. |
| Pending evidence review | Inspect the reason in Review/Audit. Fix evidence/configuration or wait for the quota cooldown, then resume. Cycle/batch caps do not reset just because you click Resume. |
| Google authorization missing | Check Desktop JSON path, consent test user and token-directory access; run the appropriate authorization command. |
| Delivery uncertain | Verify the saved Message-ID in the mailbox/provider, then reconcile in Approvals; do not blindly resend. |
| Sheets generated-cell conflict | Keep notes in M onward; restore generated cells or use the explicit acknowledgement API documented in SHEETS.md. |
| Demo destination already exists | Use a new path, or `--resume` only for a recognized demo checkpoint. |

Live account behavior and browser appearance need manual verification. Automated
UI tests cover rendering/actions, and HTTP health verifies server startup; neither
proves provider access or model judgment quality.
