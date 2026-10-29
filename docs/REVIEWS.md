# Phase 5: bounded review workflow

`ReviewEngine.step(project_id, cycle_id, batch_id=...)` advances one persisted
stage or polls its saved gateway job. It never waits for an LLM. GitHub/PDF work
can block, so call it from `ReviewRunner.background()`, not the future UI thread.
All agent specifications, including onboarding, must be registered together:

```python
from app.agents.review import all_specs
from app.agents.fake import FakeReviewClient
from app.gateway import Gateway
from app.integrations.pdf.store import PDFStore
from app.orchestrator.review import ReviewEngine
from app.scheduler.reviews import ReviewRunner

# db has been migrated; settings and github are application-owned instances.
client = FakeReviewClient() if settings.llm_mode == "fake" else None
gateway = Gateway(db, settings, all_specs(), client=client)
engine = ReviewEngine(db, gateway, github, PDFStore(db, settings))
worker = ReviewRunner(engine, batch_id="explicit-review-batch-001").background()
worker.start()
# On application shutdown:
worker.close(timeout=120)
gateway.close()
```

The caller owns the batch identifier. Keep it across ticks and restart so a
global batch cannot evade its 25-call limit. Existing cycles retain their saved
batch. The gateway remains the only provider caller; default limits are six
attempts per cycle, two coordinator attempts, six tool calls per specialist,
one JSON repair, 700 output tokens and the project daily quota. HTTP retries and
repair attempts consume calls. There is no unbounded coordinator replan loop.

Stages are `load_memory → collect → coordinator → repo_reviewer →
report_reviewer → analyze_reply → draft_followup → finalize`. Inapplicable stages
are skipped. Prior feedback checks run inside the two review stages. A typical
repository cycle needs three LLM calls; repository + report + reply + draft needs
five, leaving one default attempt for a retry/repair. Prompts, schemas, tier and
tool allowlists are separate for every role. Agent tools are application-mediated
reads; agents cannot send mail, approve deadlines, execute student code or change
budgets.

The coordinator proposes a fixed-menu plan from change counts. Code validates the
menu and enforces required evidence work. `no_action` cannot suppress a new repo,
report or reply review, and content cannot enable a tool. Proposed and effective
actions are both saved. The fake client uses explicit fixtures, never follows
instructions found in evidence, and makes no progress judgments by default.
Prompt delimiters and structural checks do not guarantee that a live model's
free-text assessment is correct; drafts still require supervisor review.

## Restart and isolation

An OS lock per database/project serializes each stage across threads/processes;
it releases on process exit. A running or pending logical cycle prevents another
cycle for the same project from starting. The runner selects the earliest
unfinished cycle per project, advances each project once per tick, and leaves
LLM fairness to the shared gateway. Proposal cycles belong to BaselineService.

Migration 005 adds review state, verified completion records, claim verification
details and reviewed-document markers, all with project-scoped foreign keys.
Gateway submission and its saved job ID commit together. Findings, feedback,
claims, proposed deadlines and draft writes commit with the stage transition.
A crash after an LLM result is saved consumes that same result after restart;
it does not repeat findings or drafts. The final transaction advances
`last_reviewed_sha`, marks matched inbound replies processed, marks documents
reviewed and records completion. Report-only and reply-only cycles never write
non-Git markers into the Git cursor.

Git reads always compare against the current saved `last_reviewed_sha`. Rewritten
or missing history uses the existing flagged tree fallback. Generated/vendor
paths, known lockfiles, binary content and README files are filtered before the
LLM. Python formatting-only changes are skipped only when both complete sources
have identical parsed syntax trees; whitespace in strings or indentation that
changes behavior is not ignored. Other languages use conservative handling.
PDF blobs are ingested through the existing bounded parser. Report hashes already
reviewed are skipped. BM25 selects at most the configured top four passages across
the new reports; citations retain page IDs and offsets.

## Evidence and decisions

Findings must cite nonempty evidence IDs both owned by the project and supplied
to that reviewer. Invalid findings or feedback references roll back the whole
stage and pause the review. Feedback status recommendations (`open`, `partial`,
`resolved`) are accepted only with supplied project evidence. These are advisory
assessments, not proof that arbitrary code works; no student tests are executed.

Completion claims without independent file/PDF citations are automatically
`unsupported`. Valid citations alone remain `unverified`. A claim becomes
`supported` only if its cited evidence includes the supervisor-confirmed
completion record for that milestone. `DeadlineService.confirm_completion(...)`
records that explicit application-level decision. Delay detection compares the
current approved due date with today (or an explicit `today=date(...)`) and checks
for a confirmed completion record. This conservative rule avoids treating the
mere presence of a file or an email assertion as completion.

Student reply dates create only proposed versions. `DeadlineService.decide(...,
approve=True)` is a supervisor operation, not an agent tool; it supersedes the
current approved version in the same transaction and appends a decision/audit.
Stale proposals cannot supersede a newer approval. Reject a stale proposal and
make a new proposal with a fresh source identifier. Deadline history is retained.

Email draft recipient and Message-ID are generated by code; recipient is the
registered student's address, and `(project, cycle, followup)` is unique. Every
draft starts pending. The [Phase 6 email service](EMAIL.md) implements SMTP/Gmail,
approval enforcement, uncertain-delivery handling and read-only reply matching.
The review controller consumes its project-matched `email_inbound` rows.

## Pending work and limits

`engine.status(...)` exposes the saved stage, jobs, effective plan and safe reason.
Quota, tool, provider and schema failures leave `pending_evidence_review`, with
the Git cursor unchanged. After the blocking condition is addressed, call
`engine.resume(project_id, cycle_id)`. It preserves all attempt/tool counters and
asks the gateway to resume an eligible pending job. Exhausted cycle/repair
allowances do not reset on resume; those require supervisor intervention/new
work, not automatic retry loops. The runner never retries pending reviews.

Repository input is split into persisted, contiguous excerpts, with
`MAX_DIFF_CHARS_PER_FILE` limiting each initial excerpt rather than discarding
the file tail. Excerpts are further split and packed against the complete prompt,
schema, metadata and repair envelope under `MAX_INPUT_TOKENS` (a conservative
UTF-8 byte proxy). Each finding must cite evidence actually supplied in its batch.
Generated directories including `.next` are ignored before blob downloads.

Review shows **Repository batches reviewed: X / Y**. Outputs and the next batch
index commit together, so a restart does not repeat completed batches. Once the
cycle cannot accommodate another request and its repair allowance, it pauses as
`review_batch_complete`. Click **Continue next review batch** to explicitly create
a new bounded cycle for the same saved review. The previous portion is labelled
`batch_complete`; it does not advance the reviewed SHA or generate an email.
Select the newest continuation cycle after refreshing. Its findings view includes
all portions. Only after all batches and remaining stages finish is the Git SHA
marked reviewed and a single follow-up draft produced. Duplicate continuation
clicks cannot create a second successor.

Continuation preserves historical calls, daily token usage and the shared batch
identifier. With defaults, each cycle is limited to six calls and each named
global batch to 25. If the global batch is exhausted, explicitly choose a new
identifier in Review before resuming; daily quota still applies. Wait for the
UTC reset for daily exhaustion. Repair limits are never reset on retry. Large
first reviews can require many cycles and more than one quota day. There is no
promise of finishing an entire repository within six calls. Small reviews retain
their single-cycle behavior.

Resuming a legacy repository job rejected as `input_too_large` retires that job
without deleting its history and builds bounded requests. If legacy file evidence
was truncated, collection is repeated against the saved commit to recover the
missing text. The existing collection tool budget still applies. No automatic
live retry or provider call is performed by the upgrade itself.

Large combined finding lists are bounded for email drafting, with explicit total
and omitted counts and a notice in the draft when it summarizes only selected
findings. All findings remain visible in Review. The gateway still refuses
oversized criteria, feedback, report or reply bundles; repository batching does
not silently omit those records. Retrieved passages intentionally cover selected
text, not a complete semantic analysis of every PDF page. Unreadable/no-match
reports remain pending. Live model accuracy and API behavior have not been tested
in this phase.

Run the isolated smoke/restart demo with:

```powershell
.\.venv\Scripts\python.exe -m scripts.review_demo
```

It uses a temporary database and fake clients, ignores `.env` file settings, and
does not send anything. See DEMO.md for the comprehensive three-project scenario.
