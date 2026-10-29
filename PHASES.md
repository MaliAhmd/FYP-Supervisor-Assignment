# Build checkpoints

## Phase 1 — complete

Implemented the Python package skeleton, validated environment configuration,
the full requested SQLite table structure, atomic checksummed migrations, WAL
connections, project-scoped persistence helpers, append-only audit/decisions,
secret-redacted metadata logging, and bootstrap/status commands.

Validation on Python 3.11.2:

- `python -m pytest -q`: **30 passed**.
- `python -m compileall -q app scripts tests`: passed.
- `python -m pip check`: no broken requirements.
- `python -m app init-db`: initialized the local database successfully.
- `python -m app status`: confirmed WAL, the initial migration, zero projects,
  and fake/fake/test integration modes.

Dependency installation required allowed network access and one retry after a
Windows file lock. Installation completed successfully. No live API was called,
and no credentials were created or stored.

Phase 1 was completed and reported before starting Phase 2.

## Phase 2 — complete

Implemented a shared, durable gateway with a Groq HTTP adapter and deterministic
fake client; Pydantic output validation; bounded JSON regeneration; conservative
input preflight; persistent call/token/tool reservations; per-model rate limiting;
bounded retries with jitter and Retry-After support; a project-scoped cache; and
a background queue that rotates fairly among eligible projects.

Queue jobs, responses, retry/repair counts, quota usage, and provider cooldowns
survive restart. An OS lock prevents duplicate workers. Exhausted work is marked
pending with a saved checkpoint while preserving its review stage. The status
API exposes queue/quota information for the future UI. No send tool is available.

Added migration `002_gateway.sql`, the fake gateway smoke/restart demo, and the
[gateway guide](docs/GATEWAY.md), including official Groq documentation links and
explicit accounting/resume semantics. No new third-party dependencies were needed.

Validation on Python 3.11.2:

- `python -m pytest -q`: **71 passed** across Phases 1–2.
- `python -m compileall -q app scripts tests`: passed.
- `python -m app init-db`: upgraded the existing Phase 1 database successfully.
- `python -m app status`: confirmed both migrations, WAL, and fake/fake/test modes.
- `python -m scripts.gateway_demo --database data/gateway_phase2_verified.sqlite3`:
  completed, with one fake provider call and one cache hit after service restart.

Tests include mocked Groq wire requests and 429 responses, exponential backoff,
daily vs per-minute limits, fairness, quotas under concurrent reservation,
project isolation, cached schema validation, input caps, prompt delimiters, tool
allowlists, a background worker, migration upgrade, and interrupted-call recovery.
No live Groq call was made; live account/model behavior remains unverified.

Phase 2 was completed and reported before starting Phase 3.

## Phase 3 — complete

Implemented the GET-only GitHub REST reader and one isolated invitation-acceptance
PATCH function, project-matched invitation discovery, access verification, and
activation after baseline confirmation. Added a deterministic fake GitHub client,
project-scoped push ingestion, persisted ETag polling, trailing-edge debounce,
deduplicated review cycles, background scheduling, and safe restart behavior.

Polling uses the SHA media type, respects conditional 304 responses and poll
interval headers, persists backoff, and shares credential rate-limit cooldowns
across projects. Expiring polling leases reject stale responses. Pending pushes
wait behind unfinished project reviews; the enqueue operation uses the latest
saved review base. Normal comparisons and force-push/missing-history tree fallback
are available for the later orchestrator. API truncation is never treated as a
complete snapshot.

Added migration `003_github_polling.sql`, an offline invitation/poll/restart demo,
a live-mode polling runner, and the [GitHub guide](docs/GITHUB.md) with official
API references and token/trigger limitations. No dependencies were added.

Validation on Python 3.11.2:

- `python -m pytest -q`: **108 passed** across Phases 1–3.
- `python -m compileall -q app scripts tests`: passed.
- `python -m app init-db`: upgraded the existing local database successfully.
- `python -m app status`: confirmed all three migrations, WAL, and fake/fake/test modes.
- `python -m scripts.github_demo`: accepted a fictional invitation, exercised an
  unchanged poll, merged two observed pushes into one queued review after local
  service restart, and detected rewritten history using the current tree.

Tests cover the GET-only boundary, isolated PATCH, ETags/304, matching invitations,
pagination, revoked access, leases, shared rate limits, burst merging, concurrent
dedupe, restart, project isolation, force pushes, malformed responses, unsafe
redirects, and upgrade of preexisting pending events. Live GitHub behavior remains
unverified; no real repository or invitation was changed.

Phase 3 was completed and reported before starting Phase 4. The optional webhook
HTTP listener remains deferred; the transport-neutral queue is ready for a future
authenticated trigger.

## Phase 4 — complete

Implemented bounded PDF parsing in a disposable subprocess; persisted PDF bytes,
page evidence and hashes; report versions and duplicate detection; and deterministic
BM25 passage retrieval with page numbers, evidence IDs, and offsets.

Added the baseline extraction prompt/schema and a persisted onboarding service:
upload, one logical extraction job, collect, edit with revision checks, and explicit
supervisor confirmation. Context selection records omitted text/blank pages, with
acknowledgement required before confirming limited coverage. Missing deadlines are
not invented. Invalid source pages stay pending. The default fake client produces
an empty draft rather than fabricated student requirements; the demo uses explicit
fictional fixtures.

Confirmation atomically saves objectives, milestones, initial approved deadline
versions, and the supervisor decision. It is idempotent, retains access status,
supersedes abandoned proposal drafts, and completes the onboarding cycle. Restart
loads the baseline directly from SQLite without re-uploading or re-parsing the PDF.
Queue insertion and draft transitions are atomic; listing drafts recovers an upload
saved before its draft was created.

Added migration `004_pdf_baselines.sql`, a real-PDF/fake-LLM demo, and the
[PDF/baseline guide](docs/PDF_BASELINE.md). Raised the pypdf minimum to the tested
6.21.2 configuration API; no additional package was needed.

Validation on Python 3.11.2:

- `python -m pytest -q`: **139 passed** across Phases 1–4.
- `python -m compileall -q app scripts tests`: passed.
- `python -m pip check`: no broken requirements.
- `python -m app init-db` and `status`: upgraded the existing database and verified
  all four migrations with WAL and fake/fake/test modes.
- `python -m scripts.baseline_demo`: one fake LLM call, supervisor-edited deadline,
  confirmed saved baseline after restart, duplicate report reuse, a new report
  version, and retrieval of relevant page 2.

Tests cover real PDF parsing, encryption/size/page/text/stream limits, safe parser
timeouts, credential isolation, no-text/manual handling, retrieval, report hashes,
project isolation, concurrent submission and confirmation, rollback, page citations,
injection delimiters, partial coverage, quota exhaustion, migration upgrade, and
restart without another upload. No live Groq extraction was performed. OCR and
Streamlit controls are not implemented; the service API is ready for the later UI.

Stopped at the requested phase boundary. Next is Phase 5: the review agents,
prompts/schemas, deterministic verification and deadlines, resumable orchestrator,
feedback checks, and bounded review cycles. This was the Phase 4 checkpoint;
Phase 5 implementation is recorded below.

## Phase 5 — complete

Implemented the four review agent prompts, strict output schemas, model tiers and
read-only tool allowlists. Added a deterministic review controller with persistent
stage snapshots, atomic gateway-job linking and result consumption, per-project
OS locks, restart recovery, and a background runner with explicit global batches.

The controller loads saved memory, compares against the reviewed Git cursor,
handles rewritten history, filters generated/binary/README and safe Python
formatting-only changes, reviews repository evidence and retrieved PDF passages,
extracts reply claims, checks prior feedback and creates recipient-locked pending
drafts. Fixed-menu coordinator suggestions cannot suppress required evidence work.
Budgets are enforced by the existing shared gateway and tool ledger. Exhaustion,
invalid evidence, unreadable reports and truncation preserve a pending checkpoint
without advancing the Git cursor.

Added project-scoped citation verification, unsupported completion-claim detection,
supervisor-confirmed completion records, deterministic delay checks, and proposed
deadline approval/rejection with superseded history and stale-proposal protection.
Valid citations alone do not establish semantic completion. Agent assessments and
draft text remain subject to supervisor review; student code is never executed.

Migration `005_reviews.sql` adds review state, completion records, claim verification
details and reviewed-document markers. Added the offline restart smoke demo and
[review service guide](docs/REVIEWS.md), including input-size and resumption limits.
The full three-project demo remains Phase 9. No dependencies were added.

Validation on Python 3.11.2:

- `python -m pytest -q`: **168 passed** across Phases 1–5.
- `python -m compileall -q app scripts tests`: passed.
- `python -m app init-db` and `status`: upgraded the local database to all five
  migrations; WAL, zero projects, and fake/fake/test modes confirmed.
- `python -m scripts.review_demo`: stopped after the saved coordinator result,
  restarted, completed the review and created one pending draft without a second
  coordinator call.

The 29 new tests cover restart and output idempotency, real background workers,
project locks and isolation, force-push fallback, triage, semantic whitespace,
report-only cycles and revisions, claim support rules, feedback references,
deadline supersession, stale decisions, quotas, tool budgets, input limits,
truncation, invalid model citations, network-failure resume and injection boundaries.
An existing migration expectation was updated for migration 005.

No live LLM/GitHub service was called and no email was sent. Stopped at the
requested phase boundary. Next is Phase 6: approved email delivery, the Gmail
reply reader, recipient enforcement, and delivery idempotency.

## Phase 6 — complete

Implemented the EmailSender interface, TLS SMTP sender and optional Gmail API
sender. Added supervisor preview/edit/approve/reject APIs with revision checks,
recipient locking, approval hashes bound to content and delivery configuration,
test-mode redirection, stable Message-IDs and durable delivery attempts. OS locks
prevent concurrent sends; interrupted or ambiguous attempts require explicit
supervisor reconciliation, and known failures require fresh approval before retry.
No agent can invoke these application-level approval/delivery APIs.

Added a GET-only Gmail reader, bounded MIME decoding and a persisted reply poller.
It matches sender addresses and outgoing In-Reply-To/References IDs, rejects
cross-project ambiguity, resumes fixed-query pagination after restart, persists
backoff, and atomically imports each reply with its review cycle. It does not
modify mailbox messages or automatically import attachments as reports.

Desktop OAuth uses separate read-only/send credentials, explicit browser
authorization, safe error messages and atomic owner-private token storage.
Windows uses a protected current-user DACL; POSIX uses 0700/0600 permissions.
Migration `006_email.sql` adds approval/attempt metadata and Gmail scan state.
Added an offline email/restart demo and the [email guide](docs/EMAIL.md), including
official Google setup references, SMTP settings, reconciliation semantics and
service examples. No dependencies were added.

Validation on Python 3.11.2:

- Full regression suite: **209 passed** across Phases 1–6.
- Final targeted email suite after Gmail transport locking: **41 passed**.
- `python -m compileall -q app scripts tests`: passed.
- `python -m pip check`: no broken requirements.
- `python -m scripts.email_demo`: approval, redirected fake delivery, no repeat
  after restart, and one matched reply/review cycle all verified.
- Windows private token writing verified outside the ACL-restricted sandbox using
  only a temporary dummy JSON file. Sandbox permission failures correctly refused
  to write credentials; no actual token was created.
- Configured workspace database upgraded directly to migration 006, with WAL and
  zero projects. Standard `init-db`/`status` was blocked by the current `.env` value
  for SMTP_FROM, which must be a single bare mailbox. The user's `.env` was left
  unchanged; direct migration did not require live mail configuration.

Tests cover approval/revision changes, header and recipient locks, test/live
routing, content/settings tampering, concurrent and repeated sending, process
interruption, database failure after provider acceptance, reconciliation, known
SMTP/Gmail rejections, TLS selection, MIME parsing, read-only Gmail boundaries,
reply matching/isolation, atomic import, pagination restart, backoff, and private
token storage failure handling. A credential-like value was removed from the
shareable `.env.example`; if real, that key should be rotated.

No real email was sent, no live account was authorized, and no live Gmail/SMTP
transport was exercised. Streamlit approval controls remain Phase 8. Stopped at
the requested boundary. Next is Phase 7: Sheets synchronization preserving manual
columns, with optional Excel export.

## Phase 7 — complete

Implemented the Sheets progress register with deterministic project summaries,
approved deadline/delay/completion calculations, generated columns A–L and manual
columns M onward. The values-only client reads before writing, reads formulas as
expressions, and submits changed generated ranges in one RAW batch update. It
never appends through the API, clears cells, resizes the grid or writes manual
columns. Project IDs identify rows across full-row sorting; duplicate/missing IDs,
edited generated cells and changed headers produce conflicts instead of overwrite.

Migration `007_sheets.sql` preserves register metadata while adding destination
binding, pending write values/hashes and safe errors. An OS lock serializes sync.
Pending records precede remote writes, allowing restart recovery after a lost
provider response or a local commit failure. Supervisor acknowledgement of an
exact observed conflict permits later regeneration, with a recorded decision.
Read/hash/write cannot provide atomic compare-and-swap against a concurrent human
editor; that limit is documented, while manual columns remain outside all writes.

Added separate Sheets OAuth credentials using the shared owner-private token
storage; no Gmail scopes were expanded. Added standalone Excel export with literal
text cells, frozen headers, filtering and blank manual columns. Exports refuse to
overwrite existing workbooks. Added the live sync/local export command, explicit
authorization command, offline fake-register restart demo, and
[register guide](docs/SHEETS.md). No dependencies were added.

Validation on Python 3.11.2:

- Full regression suite: **236 passed** across Phases 1–7.
- The 27 new tests cover manual columns, one-batch writes, formula/numeric/blank
  conflicts, row sorting and identity conflicts, capacity, project isolation,
  destination binding, acknowledgement checks, pending reservations, lost responses,
  database failure after acceptance, concurrent locks, adapter wire contracts,
  literal Excel text, no-overwrite exports and legacy migration preservation.
- Existing Gmail/email tests passed after extracting the shared scoped OAuth loader.
- `python -m scripts.sheets_demo`: one initial batch, preserved manual notes,
  reported generated-cell conflict, and restart recovery without repeating a write.
- `python -m compileall -q app scripts tests` and `pip check`: passed.
- Sync/export command help verified without network activity.
- Configured workspace database upgraded directly through migration 007 with WAL
  and zero projects. Standard init/status remains blocked by the existing invalid
  SMTP_FROM value in `.env`; that user configuration was not changed.

No live Google authorization or spreadsheet update was performed. Stopped at the
requested phase boundary. Next is Phase 8: Streamlit pages and background-worker
integration for the implemented services.

## Phase 8 — complete

Implemented the local Streamlit application with Setup, Projects, Dashboard,
Review, Approvals, History, Register and Audit pages. Added guided proposal upload,
baseline editing/confirmation, invitation acceptance/access verification, project
editing/pause controls, report upload, opt-in GitHub/reply monitoring, review-now
and resume actions, live stage/task status, evidence text/commit links/PDF downloads,
email editing/approval/rejection/send/reconciliation, deadline decisions, completion
confirmation, register sync and persisted Excel downloads. Sidebar meters expose
queue depth, project jobs, daily usage and quota-reset time.

Migration `008_ui.sql` adds project-scoped durable UI tasks, monitoring settings and
fake commit history. A shared locked runtime owns the gateway and background worker;
Streamlit rendering never performs PDF parsing, delivery, provider reads, sync or
export generation. Queued actions survive restart. Interrupted remote actions are
flagged rather than blindly replayed; subsystem checkpoints and uncertain-delivery
records retain their existing safeguards. Fake repository fixtures replay from
SQLite after restart. No authoritative state depends on browser session state.

Added `streamlit_app.py`, loopback-only Streamlit defaults with XSRF protection,
safe configuration-error rendering, non-sending SMTP connection checks and the
[UI operating guide](docs/UI.md). Raised the Streamlit minimum to the installed,
tested 1.66.1 version. OAuth remains an explicitly invoked terminal flow; no
credentials are displayed or edited by the UI. The existing `.env` was not changed.

Validation on Python 3.11.2:

- Full regression suite after fixing the example configuration: **258 passed**.
- The 22 new Streamlit/runtime tests cover every page, project creation, safe
  invalid-configuration rendering, queued review actions, actual PDF upload and
  baseline confirmation, fake review restart completion, interrupted-action
  non-replay, persisted monitoring, approval without sending, deadline history,
  PDF/Excel download rendering, task isolation/deduplication and SMTP probes.
- A local headless Streamlit server successfully answered its health endpoint on
  127.0.0.1 and was stopped after verification. AppTest exercised the UI flows;
  visual browser layout has not been manually verified.
- `python -m compileall -q app scripts tests streamlit_app.py`: passed.
- `python -m pip check`: no broken requirements.
- Configured workspace database upgraded directly through migration 008 with WAL
  and zero projects. `.env` still contains an invalid SMTP_FROM; the UI reports it
  safely and waits for correction instead of starting services.

The first regression run found invalid SMTP_FROM in the shareable `.env.example`.
Restored its valid placeholder and cleared a populated SMTP_PASSWORD. Added an
assertion that example secret fields remain empty. If that password was real, it
should be rotated. No actual email, OAuth authorization, Google update or live
GitHub action was performed during this phase.

Stopped at the requested phase boundary. Next is Phase 9: the comprehensive
three-project, three-cycle seed/demo and restart scenario.

## Phase 9 — complete

Implemented `scripts.seed_demo` with three fictional projects, two milestones
each and three review rounds. Onboarding uses real generated PDFs, the baseline
service and one fake extraction per project. Reviews exercise the application
queue/debounce, shared gateway, evidence verification, feedback progression,
recipient-locked fake email delivery, reply matching, deadline approval/version
history, report deduplication/revision, register sync and Excel export.

The scenarios include a delayed project, an unsupported completion claim, a
student-proposed extension explicitly approved by the fixture supervisor with
the old date retained, and two report hashes with relevant page-two citations.
The fixed scenario date is 2026-10-26. The fake fixtures make no claims about
live-model quality and no student code is executed.

The default runner closes its services after round two and starts a fresh Python
interpreter for round three. Separate `--stop-after-2` / `--resume` commands also
demonstrate a full stop between invocations. The new process reads baselines,
reviewed SHAs, feedback, claims and approved dates from SQLite and reconstructs
only fake Git history from the manifest. Completed runs can be verified again
without new reviews, provider attempts, drafts or exports. Existing or unrecognized
destinations are not overwritten. The driver resumes only its declared checkpoint;
it does not guess how to repair arbitrary partially seeded runs.

Added the [full demo guide](docs/DEMO.md), including output descriptions, budget
accounting, explicit live-test-repository instructions and the distinction between
offline fake delivery and real redirected test email. No dependencies, production
services or database migrations were added. Demo settings explicitly override
both `.env` and live integration environment variables.

Validation on Python 3.11.2:

- Full regression suite: **262 passed** across Phases 1–9.
- Four new tests cover automatic new-process restart, manual stop/resume, completed
  rerun deduplication, normal budget caps, retained state, environment isolation,
  report/claim/feedback outcomes and refusal to overwrite existing directories.
- `python -m compileall -q app scripts tests streamlit_app.py`: passed.
- `python -m pip check`: no broken requirements.
- `python -m scripts.seed_demo --directory data/phase9_verified`: completed all
  rounds, including the automatic child process. Outputs include the demo SQLite
  database, manifest, checked summary and Excel register.
- Verified output: 3 projects, 6 milestones, 9 completed review cycles plus 3
  onboarding cycles, 9 drafts, 8 findings, 2 report versions and 1 unsupported claim.
  Thirty LLM log records comprise 28 fake provider attempts and 2 cache hits.
- Re-running a completed demo with `--resume` verified it without repeating work.

The first automatic restart exposed a Windows child-environment issue; preserving
the home-directory variables fixed it. A test initially counted cache hits as
provider attempts and was corrected to verify both counters explicitly.

No live LLM/GitHub/Gmail/SMTP/Sheets account was contacted and the production
database was not changed. Stopped at the requested phase boundary. Next is Phase
10: final test pass, consolidated setup documentation and requirements checklist.

## Phase 10 completed — final validation and handoff (2026-10-27)

Updated README with a safe first-install flow that preserves an existing `.env`,
run/test commands and links to the consolidated [setup guide](docs/SETUP.md) and
[sections 1–8 requirements checklist](docs/CHECKLIST.md). The setup guide covers
Groq configuration, external GitHub OAuth token provisioning, Google Desktop OAuth
and test users, SMTP app passwords, offline/browser/live testing and recovery.
The checklist records implemented behavior and explicit limitations rather than
claiming live verification, an optional webhook or an embedded GitHub OAuth flow.
Corrected obsolete future-UI wording in the integration guides.

Added `python -m scripts.check_env`, a read-only offline configuration checker.
It reports known invalid field names, configured-value presence and local Google
file prerequisites without displaying credentials, addresses, file contents or
raw exceptions. Three tests cover secret-safe valid/invalid output, malformed
Google JSON and absence of token writes.

Final validation on Python 3.11.2:

- **265 tests passed** in the combined regression suite (123.15 seconds).
- `python -m pip check`: no broken requirements.
- `python -m compileall -q app scripts tests streamlit_app.py`: passed.
- Fresh `scripts.seed_demo --directory data/phase10_verified`: all three rounds
  completed, including a real subprocess restart after round two.
- Demo outputs: 3 projects, 6 milestones, 9 reviews plus 3 onboarding cycles,
  9 drafts, 8 findings, 2 report versions, 1 unsupported claim, 28 fake provider
  attempts and 2 cache hits. Summary and Excel export were produced.
- Completed `--resume`: verified with no repeated cycles, calls or drafts.
- Local links in README, SETUP and CHECKLIST resolve.

The user's updated `.env` was inspected without revealing values or contacting
providers. It remains unchanged. At this review, `SMTP_FROM` contains a display
name/whitespace and fails bare-mailbox validation, preventing configured startup.
Both Groq model names are blank, the test redirect is still an example mailbox,
the Google client JSON path does not exist and Google token files are absent.
LLM/GitHub modes are fake and delivery is SMTP in test mode. `SMTP_SECURE` is an
ignored setting; the supported selector is `SMTP_TLS_MODE`. The shareable
`.env.example` secret fields are empty. Filling credentials does not verify
their validity; live acceptance checks remain the supervisor's next step.

No live model/GitHub/mail/Sheets request or delivery was performed, and no
production project data was modified. All ten requested build phases are complete;
the documented configuration fixes are required before this user's app startup.

## Follow-up: resumable repository batches (2026-10-27)

Replaced the single oversized repository request with deterministic packing of
project-owned evidence. Files are retained in contiguous excerpts, further split
against the actual schema/prompt/metadata/repair envelope. Each result and next
batch index commit atomically. Oversized legacy pending jobs can be retired and
repacked on explicit resume; previously truncated files are recollected against
the saved commit. `.next` generated output is filtered before downloads.

Explicit continuation creates a new bounded cycle, keeping historical attempts,
shared-batch usage and daily quota intact. The prior cycle is labelled
`batch_complete`. Only the final portion advances the reviewed SHA and produces
the follow-up draft. Findings across all portions are shown together. Drafting
uses bounded selected findings with an explicit omission count/notice; it does
not delete findings. Non-repository contexts can still exceed the input cap.

Added UI batch progress, a continuation action, and actionable quota messages.
Updated review documentation and the final checklist. The local `.env` input cap
was increased from 3000 to 5000; all other settings were preserved, including
6000-token local pacing, 700 output tokens and existing call/daily quotas. Code
and template defaults remain 3000. Planning on a disposable copy of the saved
database produced 50 requests rather than 351; full recollection may add more.
No live repository content was sent to a model during these changes.

Validation: **278 tests passed** (109.23 seconds), including lossless file slicing,
bounded prompts, restart and continuation, no duplicate findings/drafts, preserved
cycle/batch caps, legacy job upgrade and UI continuation. Compilation passed.
The first full run found a new test using the wrong UI-task column name; fixing
that test was followed by the successful full run. The live user review remains
pending until an explicit resume after restarting Streamlit.
