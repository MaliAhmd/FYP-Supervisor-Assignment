# SupervisorAI

Local supervision of student FYP/thesis projects, built in ten phases with a
deterministic Python controller, SQLite memory, bounded agents, and supervisor
approval before email delivery.

**Phase 10 complete: local application, offline demo, setup guide and final checklist.**
Start with the [setup and testing guide](docs/SETUP.md). The
[requirements checklist](docs/CHECKLIST.md) maps sections 1–8 to implementation,
tests and known limitations. Live provider credentials and behavior require your
own account checks; the automated suite uses fake or mocked services.

## Run the current build

Requires Python 3.11 or newer. Run commands from this repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m scripts.check_env
.\.venv\Scripts\python.exe -m app init-db
.\.venv\Scripts\python.exe -m app status
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

On macOS/Linux, use `.venv/bin/python` and `cp .env.example .env` instead.
Do not overwrite an existing `.env`; adjust its values instead. If a managed
environment disables package indexes with `PIP_NO_INDEX`, installation requires
an allowed package source/network connection. The source has no vendored wheels.

`init-db` creates `data/supervisorai.sqlite3`, applies migrations atomically, and
enables WAL. Running it again preserves existing records. `status` prints only
database location, migration names, project count, and integration modes; it never
prints settings or credentials. These commands do not call external services.
The gateway demo uses `data/gateway_demo.sqlite3`, restarts the service, and
demonstrates a persisted cache hit. Repeating it deduplicates its existing jobs.
This is a gateway smoke demo; `scripts.seed_demo` runs the full review scenario.

Open `http://127.0.0.1:8501`. Stop with `Ctrl+C`; restart after editing `.env`.
The environment checker is offline and never prints configuration values. A valid
configuration is not proof that a provider accepts the credentials. Fix reported
validation errors before starting the app.

## Test the build

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q app scripts tests streamlit_app.py
$demoPath = "data/demo-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff')"
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory $demoPath
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory $demoPath --resume
```

The demo ignores `.env`, uses no live services, restarts in a fresh process after
round two, and writes `summary.json`, `manifest.json`, a SQLite database and
`progress.xlsx`. Completed resume verifies deduplication without repeating work.
See [manual UI and live test steps](docs/SETUP.md#test-in-the-browser) and the
[scenario guide](docs/DEMO.md).

## Configuration and defaults

Settings are read from the root `.env` and environment variables, with environment
variables taking precedence. Relative database paths resolve from the repository
root, independent of the working directory. See [.env.example](.env.example) for
integration settings and budget defaults.

- `LLM_MODE=fake`, `GITHUB_MODE=fake`, and `EMAIL_MODE=test` are defaults.
  Both fake LLM and fake GitHub clients are implemented.
- Groq model names have no default. Live mode requires a key and both model names.
  These names must be selected from the provider's currently available models.
- SMTP defaults to STARTTLS on port 587; implicit TLS is also configurable.
- OAuth token storage defaults to `~/.supervisorai/tokens`, outside this source
  tree. Configuration rejects a token directory inside the source tree. Gmail
  credentials use separate read/send scopes and owner-only storage; authorization
  is an explicit action. See [email setup](docs/EMAIL.md).
- Poll every 60 seconds, poll replies every 300 seconds, debounce for 300 seconds.
- Budgets default to 6 LLM calls per cycle, 25 per batch, 2 coordinator calls,
  6 specialist tool calls, 3,000 input tokens, 700 output tokens, one JSON repair,
  and 30,000 tokens per project per UTC day. The gateway enforces these call/tool
  limits with persistent reservations integrated into the review stages.
- Local model pacing defaults to 10 requests and 6,000 tokens per minute. These
  are configurable application limits, not assertions about Groq's free tier.
  Provider headers and 429 cooldowns can further restrict them.
- Timestamps use UTC. Milestone dates are calendar dates; delay detection uses the
  host's local date unless the caller supplies an explicit date.

The `.env`, token directory, runtime data, logs, and virtual environment are
gitignored. Credentials use Pydantic `SecretStr`. Do not serialize raw secret
values or place credentials inside evidence or arbitrary metadata strings.

## Database design

[migrations/001_initial.sql](migrations/001_initial.sql) creates every table named
in the requested schema. Project-owned tables all carry `project_id`;
`projects.id` is the root. `schema_migrations` is the sole application-level
metadata table and has no project owner. SQLite's own `sqlite_sequence` is managed
by SQLite.

Composite foreign keys reject references to another project's cycle, milestone,
deadline, or evidence. Triggers also check evidence IDs embedded in finding and
feedback JSON arrays. The project repository scopes evidence, cache, checkpoints,
and audit queries to its bound project. Supervisor dashboard enumeration is an
explicit `list_projects` operation and must never be exposed as an agent tool.

Schema guarantees include:

- One approved deadline per milestone and retained historical versions.
- Append-only audit log and supervisor decisions; deadline rows cannot be deleted
  or have their historical values changed.
- Deduplicated push events, review cycles, evidence, report hashes, and email keys.
- At most one running cycle per project, with OS locks and persisted stage/job
  recovery in the review worker. See the [review guide](docs/REVIEWS.md).
- Atomic checkpoint payload/stage updates and cache entries scoped by project.
- Email approval timestamps required for approved/sent rows, with space reserved
  for uncertain SMTP delivery. Database constraints alone do **not** implement
  approval authorization or exactly-once delivery; the email service supplies the flow.

Each connection enables foreign keys, a 30-second busy timeout, and full
synchronous durability. Connections are short-lived and not shared across
threads. Migrations are serialized using `BEGIN IMMEDIATE`; each applied script
has a checksum. Do not edit an applied migration. Add a new numbered SQL file.
Migration execution deliberately avoids `executescript` and its implicit commit.

## Phase 2 gateway

See [the gateway guide](docs/GATEWAY.md) for the Python API, persistent queue,
fair scheduling, budget/reset semantics, conservative token estimates, retry and
repair rules, live configuration, and links to the official Groq API documentation.

Migration `002_gateway.sql` upgrades the Phase 1 database without modifying its
applied migration. The gateway exposes queue/quota status to the
UI and pauses exhausted work with a saved checkpoint. Groq HTTP behavior is tested
with mocked HTTP responses; no real provider call has been made.

## Phase 3 GitHub integration

See [the GitHub guide](docs/GITHUB.md) for invitation/access handling, ETag polling,
debounce semantics, force-push handling, token requirements, and limitations.
The reader only issues GET requests; one isolated supervisor-invoked function can
PATCH an invitation acceptance. The fake demo exercises invitation acceptance,
unchanged polling, burst merging, and restart without accessing GitHub.

Migration `003_github_polling.sql` adds persisted polling/invitation state. The
poller queues review cycles but does not execute agents or advance reviewed SHAs.
Live GitHub behavior remains untested; the optional webhook listener is deferred.

## Phase 4 PDF ingestion and baseline confirmation

See [the PDF/baseline guide](docs/PDF_BASELINE.md) for parsing limits, page-based BM25
retrieval, the one-job extraction flow, visible coverage limits, supervisor editing,
and atomic confirmation. The baseline demo uses a real generated PDF and a fake
LLM, then verifies saved memory across restart and report revision/deduplication.

Migration `004_pdf_baselines.sql` adds persisted source documents, page mappings,
editable drafts, and project objectives. Confirmed baselines no longer require
the proposal upload. GitHub activation remains a separate step. The UI
calls these services from its background worker. pypdf requires version
6.21.2 or later within major version 6 for the tested parser-limit configuration API.

## Phase 5 review agents and orchestration

See the [review guide](docs/REVIEWS.md) for agent contracts, worker setup,
checkpoint recovery, evidence verification, supervisor deadline decisions, and
pending-budget behavior. `scripts.review_demo` runs an isolated offline review,
restarts after a saved coordinator result, and creates one pending email draft.
The email service below adds approval and delivery.

Repository reviews now pack evidence into durable, bounded requests and split
large files without discarding their tails. Review shows batch progress. When a
cycle reaches its allowance, **Continue next review batch** explicitly starts
another bounded cycle while retaining daily/shared-batch usage. Only the final
portion advances the reviewed Git SHA and produces the follow-up draft. See
[review batching and recovery](docs/REVIEWS.md#pending-work-and-limits).

## Phase 6 email and reply ingestion

See [email setup and service APIs](docs/EMAIL.md) for SMTP app passwords, separate
Gmail read/send OAuth, private token storage, draft approval, uncertain delivery
reconciliation, and the reply worker. The offline `scripts.email_demo` verifies
approval, test redirection, restart deduplication, and reply queueing without
contacting a real mailbox. `EMAIL_MODE=test` redirects actual provider delivery;
it is not a dry run. No live delivery was performed during development.

## Phase 7 Sheets register and Excel export

See the [register guide](docs/SHEETS.md) for Google setup, the generated A–L column
schema, manual-column preservation, conflict handling and restart recovery.
`scripts.sheets_demo` runs offline. The explicit `scripts.sync_sheets --sync`
command updates the configured spreadsheet; `--excel data/progress.xlsx` creates
a new local snapshot without Google access. Existing Excel files are never
overwritten. Live Google Sheets behavior has not been exercised.

## Phase 8 local supervisor workspace

Launch `streamlit_app.py` using the command above and open the localhost address.
The app includes Setup, Projects, Dashboard, Review, Approvals, History, Register
and Audit pages, with durable background actions and live task/stage progress.
See the [UI operating guide](docs/UI.md) for onboarding, monitoring, approvals,
fake fixtures, restart behavior and local-only hosting. Configuration errors are
shown without secret values; fix `.env` before starting the workspace services.
`SMTP_FROM` must contain a single bare email address.

## Phase 9 complete offline scenario

`python -m scripts.seed_demo` creates an isolated new `data/phase9_demo` directory
with three fictional projects, two milestones each and three review rounds. It
starts a fresh interpreter after round two and checks saved memory, delay flags,
deadline supersession, report revisions, unsupported claims and deduplication.
It ignores `.env` and live integration environment variables; no real email or API
call is made. Existing destinations are refused. Use `--directory` for another
fresh run, or `--resume` to verify/resume a recognized demo.

See the [full demo guide](docs/DEMO.md) for separate stop/resume commands, output
artifacts, normal budget limits, and instructions for equivalent real test repos.

## Logging

Application logs emit structured event metadata, omit raw messages and exception
text, and recursively redact credential-named keys and `SecretStr` values. Do not
log repository content, PDF passages, email bodies, HTTP payloads, or arbitrary
exception messages. Project audit events use the same metadata redaction and
remain in SQLite across restarts. This is local integrity protection, not a
security boundary against someone with direct filesystem/database access.

## Layout and build phases

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Skeleton, config, migrations, audit/logging | Implemented |
| 2 | Shared Groq/fake gateway, budgets, rate limits, caching | Implemented |
| 3 | GitHub GET-only client, invitations, ETag poller, debounce | Implemented |
| 4 | PDFs, retrieval, proposal baseline extraction | Implemented |
| 5 | Agents, schemas, verification, checkpointed orchestration | Implemented |
| 6 | Approved email delivery and Gmail replies | Implemented |
| 7 | Sheets register and Excel export | Implemented |
| 8 | Streamlit pages and background worker | Implemented |
| 9 | Three-project demo and restart scenario | Implemented |
| 10 | Full integration tests, setup guide, final checklist | Implemented |

The `app/` packages follow the requested layout. `prompts/` includes all five
agent prompts. `scripts/` includes gateway, GitHub, baseline, review restart, and
email and register smoke demos plus the full scenario. `tests/` verifies Phases 1–10,
including persistence across reopening the database, transaction rollback,
concurrent startup, migration checksums, cross-project reference rejection,
cache isolation, deduplication, immutable history, and secret-safe metadata logs.
Gateway coverage includes fair scheduling, background work, bounded repair/retries,
mocked 429 handling, quotas, tool allowlists, request isolation, cache validation,
injection delimiters, concurrent reservations, and persisted restart/cooldown state.

The email guide documents Google Cloud test users/consent and SMTP setup using
official sources. The demo guide describes real-repo scenarios; live account
behavior remains unverified.
