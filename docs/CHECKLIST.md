# Phase 10 requirements checklist

Reviewed against sections 1–8 of the original specification on 2026-10-27.
Implemented means present in the local application and exercised by automated
fake/mocked tests; it does not mean verified against live accounts.

## 1. Product behavior

| Requirement | Result and evidence |
| --- | --- |
| Dynamic projects, students, milestones and deadlines | Implemented in SQLite and Projects UI; `test_database.py`, `test_ui.py`. No seeded projects are required for ordinary use. |
| PDF extraction, supervisor editing and confirmed baseline | Implemented; `test_pdf_baseline.py` checks extraction, revisions, coverage acknowledgement, confirmation and restart without re-upload. One logical extraction job may use bounded retries/repair. |
| Discover/accept collaborator invitation and verify access | Implemented; `test_github.py` checks invitation/project matching, explicit acceptance and activation. OAuth token provisioning is external. |
| ETag polling, dedupe and trailing debounce | Implemented; default debounce 300 seconds, durable queue and transport-neutral trigger boundary. Optional webhook receiver is not included. |
| Locked, checkpointed review stages | Implemented in `app/orchestrator/review.py`; `test_reviews.py` covers restart, locks, old/new SHA, force push, triage, feedback, replies and drafts. |
| Gmail replies matched by Message-ID references | Implemented with read-only scope, sender checks and duplicate suppression; `test_email.py`. No Gmail mailbox mutations. |
| Deterministic delayed milestones | Implemented; past approved date with no supervisor-confirmed completion. Calendar date is host-local unless explicitly supplied. |
| Proposed/approved/superseded deadline history | Implemented, supervisor-only decisions; `test_reviews.py`, `test_ui.py`, `test_seed_demo.py`. |
| Approved SMTP email and selectable Gmail API sender | Implemented; separate approve/send, recipient lock and uncertain-delivery reconciliation; `test_email.py`, `test_ui.py`. |
| Sheets generated columns, hashes and Excel | Implemented; A:L only, one batch, M onward preserved, read/hash conflicts, exclusive Excel creation; `test_sheets.py`, `test_sheets_additional.py`. |

## 2. Hard rules

- Python 3.11+, Streamlit, SQLite WAL, Groq HTTP gateway, pypdf, httpx,
  smtplib, Google libraries, Pydantic v2, scheduler and pytest are used.
- Deterministic application code selects stages and enforces budgets. Agent
  tool allowlists contain read tools only. No student code is executed.
- GitHub reader issues GET only; invitation acceptance is the single isolated
  PATCH. `test_client_has_no_write_methods_and_only_one_isolated_patch` checks it.
- Every prompt has the untrusted-data rule, schema and runtime budget envelope.
  Injection and effective-plan tests verify content cannot grant tools or alter caps.
- Project-owned tables and service queries are scoped; composite keys and evidence
  checks reject cross-project references. Global scheduler/quota enumeration is
  supervisor/application-only. Migration metadata is global, not student memory.
- Credentials are local, secret fields masked, logs redacted, token storage outside
  source with restrictive permissions. `test_logging.py`, OAuth tests and
  `test_check_env.py` cover these boundaries. No secrets belong in `.env.example`.
- SMTP/Gmail delivery requires code-checked approval, a locked registered recipient,
  stored Message-ID and unique draft key. Test mode redirects real mail.
- Model IDs and pacing are configured; fake LLM/GitHub clients support offline tests.
  Free-tier availability/limits are provider-controlled, not guaranteed by the app.

## 3. Agent design and deterministic checks

Coordinator, repo reviewer, report reviewer and claims/comms each have their own
prompt, schema and allowlist; baseline extraction has a separate fifth prompt.
The coordinator menu is fixed. Repo input is filtered/capped, reports use BM25
top-k (default 4) with page evidence, and claims/comms produce structured outputs.
Application code handles memory, triage, evidence IDs, deadline versions, sending
and register writes. Evidence is data, never executable instructions.

Defaults are 6 cycle calls, 25 batch calls, 2 coordinator calls, 6 specialist tool
calls, 3,000 estimated input tokens, 700 output tokens (maximum 800), one JSON repair
and 30,000 daily project tokens. Pending evidence state is persisted on exhaustion.
`test_gateway.py` and `test_reviews.py` check attempted-call accounting and resumes.

Citation validity proves provenance, not semantic completion. A completion claim
without independent valid evidence is unsupported; valid references remain
unverified until supervisor-confirmed completion. Feedback judgments are advisory
model results with structural evidence validation. Repository evidence now uses
durable bounded batches and explicit continuation cycles (see REVIEWS.md).
Oversized non-repository context is still refused. Resume retains consumed
budgets; continuation creates a separate bounded cycle and preserves daily and
shared-batch usage. Only the final completed portion advances the reviewed SHA.

## 4. LLM gateway

| Requirement | Automated evidence in `test_gateway.py` |
| --- | --- |
| One shared gateway, fair queue and persistent jobs | Round-robin, background worker, recovery and concurrency tests |
| Input/output limits and durable budget reservations | Cycle/batch/coordinator/tool/daily limits and concurrent reservations |
| Rate headers, mocked 429, backoff and cooldown | Retry-After/header parsing, retry outcomes and persisted rate-limit tests |
| Model tiering and project-scoped cache | Agent specs, cache hit, project/evidence isolation and cache validation |
| JSON schema validation and bounded repair | Invalid output/repair tests; every attempt is counted |
| Metadata logs and UI status | Token/latency/cache/outcome records; `test_ui.py` covers page rendering |

Token preflight uses a conservative UTF-8 byte proxy rather than an exact provider
tokenizer. Cached responses and fake outputs do not establish live-model quality.

## 5. Authoritative SQLite memory

Migrations 001–008 include all requested entities plus durable jobs, PDF pages,
baseline drafts, confirmations, delivery attempts, polling/sync state and UI tasks.
WAL, foreign keys, checksummed atomic migrations, immutable decisions/audit,
single approved deadline and unique push/cycle/draft keys are implemented.
`test_database.py` covers constraints, upgrades, rollback and isolation; each
feature's tests cover its added migrations and transactional behavior.

## 6. Streamlit UI

Setup, Projects, Dashboard, Review, Approvals, History, Register and Audit are
implemented. Background tasks and monitoring settings persist in SQLite;
`st.status` and sidebar meters expose progress/usage. Evidence has commit links
or PDF downloads/page labels. `test_ui.py` uses Streamlit AppTest for all pages,
onboarding, reviews, approval separation, deadline history and restart.

Unsaved form/navigation state is ephemeral; authoritative state is durable.
Configuration is edited in `.env`, and OAuth authorization uses explicit terminal
commands. The app has no multi-user authentication and binds to loopback. Automated
UI tests do not replace manual visual/browser acceptance testing.

## 7. Restart, dedupe and isolation

- Baselines, feedback, claims, SHAs and checkpoints survive restart. The seeded
  demo uses a fresh process between rounds two and three (`test_seed_demo.py`).
- Same SHA/hash/reply/draft keys suppress duplicate processing; completed resumes
  do not repeat model attempts or send calls. Interrupted side-effect UI tasks
  are marked interrupted rather than blindly replayed.
- Project-scoped evidence, cache, drafts, reports and decisions have service and
  database isolation tests across feature suites.
- Exactly-once external delivery is not possible across SMTP/Google and SQLite.
  Ambiguous sends remain uncertain until the supervisor verifies delivery.
- Sheets read/hash/write is not atomic against a concurrent human editor. Manual
  columns stay outside write ranges, but generated-column edits can race a sync.
- Polling observes heads at intervals; it cannot recover an unobserved push that
  is reverted before the next poll. The optional webhook listener is deferred.

## 8. Demo and required test cases

`scripts.seed_demo` implements three fictional projects, two milestones each,
three review rounds, a delay, approved extension/supersession, revised report,
unsupported completion claim and real process restart. It uses real PDF parsing,
SQLite and Excel export with fake network services. [DEMO.md](DEMO.md) documents
the equivalent real-repository exercise; [SETUP.md](SETUP.md) gives run/test steps.

| Required test | Coverage |
| --- | --- |
| Deadline supersede/history | `test_deadline_history_stale_proposal_and_completion` |
| Evidence validation/unsupported claims | `test_cross_project_findings_rejected_atomic`, `test_reply_unsupported_and_processed_only_at_finish` |
| Idempotent send, recipient lock and redirect | `tests/test_email.py` |
| GET-only GitHub, ETag 304, debounce, force push | `tests/test_github.py` |
| Budget caps and graceful pending | `tests/test_gateway.py`, `test_exhaustion_preserves_cursor_and_checkpoint` |
| Mocked 429 and cache hit | `tests/test_gateway.py` |
| Cross-project isolation | Database, gateway, review, PDF, email and Sheets suites |
| Preserve manual Sheet cells | `test_batch_once_and_preserve_manual_columns` plus recovery/race cases |
| Prompt injection cannot change effective plan | `test_injection_cannot_change_tools_or_effective_plan` |

## Validation limits

The final suite, dependency check, compilation and offline restart demo are run
locally; see [PHASES.md](../PHASES.md) for results. Live Groq/GitHub/SMTP/Gmail/Sheets
access, real email delivery, account permissions and browser appearance remain
manual acceptance steps. Google authorization and actual mailbox credentials
must be supplied locally. No optional webhook or embedded GitHub OAuth flow is
claimed as implemented.
