# Phase 8: local Streamlit workspace

Run from the project root:

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Open the local address printed by Streamlit, usually `http://127.0.0.1:8501`.
The repository's `.streamlit/config.toml` binds to loopback, enables XSRF protection,
disables usage telemetry and caps browser uploads at 20 MiB. This is a local
single-supervisor application with no multi-user authentication. Do not override
the address to expose it on a public/network interface.

The app reads the existing `.env` and migrates SQLite on valid startup. Do not copy
the example over an existing `.env`. Configuration errors display field names and
safe validation messages, not submitted values. In particular, `SMTP_FROM` must
be a single bare mailbox such as `supervisor@example.com`, without a display name,
angle brackets or header text. Fix it locally and click **Reload configuration**.
Once workers have started, restart Streamlit after changing configuration; it
does not silently replace credentials or delivery settings under running work.

Streamlit 1.66.1 or later within major version 1 is required (the version tested).
All service APIs remain callable without Streamlit. No browser, OAuth consent,
SMTP connection or real GitHub mutation is launched merely by importing UI code.

## Pages and normal workflow

**Setup** shows configured integration modes and token presence, distinguishes
configuration from verified access, and provides explicit Gmail-read, SMTP
TLS/authentication and selected-repository checks. SMTP checks never submit mail.
The OAuth commands shown there are run explicitly from a terminal to connect the
supervisor's Google account. See [email setup](EMAIL.md) and [Sheets setup](SHEETS.md)
for credentials, scopes and provider settings. The UI does not edit `.env` or echo
existing secrets.

**Projects** provides the onboarding steps:

1. Add the student name, registered email and `owner/repository`.
2. Upload the proposal and click **Save proposal**. Parsing runs in the background;
   its task record and bytes are persisted first. Refresh saved state when finished.
3. Request extraction, or enter the baseline manually. Edit objectives one per line
   and add/remove milestones in the table, with criteria and ISO calendar dates.
4. Save edits, inspect the saved baseline, acknowledge coverage and explicitly
   confirm it. Confirmation saves authoritative memory; restart never needs the
   proposal again. Confirmation uses the saved draft, not unsubmitted form edits.
5. Discover and explicitly accept a matching collaborator invitation, then verify
   access. Existing access can be verified without an invitation. Repository changes
   are allowed only before baseline confirmation. Pausing/resuming is explicit;
   resumed projects need access verification again. Changing the registered email
   clears approval on unsent drafts. In-flight/uncertain mail blocks these edits.

In fake GitHub mode, **Create sample code commit** creates a deterministic local
fixture. Fake commits are saved and replayed after restart; verifying access refers
only to that fake repository. Default fake LLM extraction returns an empty draft,
so enter actual objectives/criteria/dates yourself. Fake review responses make no
progress judgments. Fake GitHub/LLM modes do **not** turn SMTP or Google into fakes.

**Dashboard** summarizes projects, status, push observations, delayed milestones,
open feedback, latest review and reviewed SHA from SQLite.

**Review** saves opt-in monitoring settings and an explicit shared batch identifier.
Monitoring polls GitHub, applies debounce, and advances queued reviews in the
background. The batch identifier persists across restart; existing cycles retain
their gateway budgets. **Review now** reads the current head and queues an unchanged-
SHA-idempotent cycle. Report uploads create report cycles. Enable monitoring to
process report/reply cycles automatically. A separate checkbox opts into periodic
read-only Gmail reply polling; the existing persisted reply cooldown governs its
slower cadence. **Read matching Gmail replies** requests a single poll.

Saved stage progress appears in `st.status` and updates every two seconds, along
with background task status. The sidebar shows queue depth, daily tokens and UTC
quota-reset time. Other page content updates with **Refresh saved state**. Findings
show project-scoped evidence text, GitHub commit links and PDF source downloads
with page numbers. Pending reviews expose **Resume saved review**; resumption does
not reset budgets. Repeated input-cap or exhausted-cycle work requires addressing
the underlying cause rather than repeatedly clicking resume.

**Approvals** separates edit, approve/reject and send actions. It displays both the
registered recipient and actual delivery destination. **Approve this text** saves
the displayed edits and approves that revision; it never sends. **Send approved
email** queues application delivery, which rechecks the approval hash and recipient
lock. Test mode redirects real delivery. Sent drafts are read-only. Uncertain
attempts require a note and an explicit verified delivery resolution before retry.
Proposed deadlines have independent approve/reject controls that retain history.

**History** displays deadline versions, decisions, claims and feedback with evidence.
The supervisor can propose a deadline change and then decide it in Approvals.
Milestone completion requires selecting and inspecting project evidence and an
explicit confirmation that it satisfies the criteria. Merely having a citation
does not automatically complete a milestone.

**Register** previews all projects, queues explicit Google sync and prepares an
Excel download in the background. It shows sync/conflict metadata. Manual columns
are never submitted to Sheets. For deliberate conflict acknowledgement, use the
reviewed service API in [SHEETS.md](SHEETS.md); there is no one-click force overwrite
in this UI. Excel artifacts are persisted as task outputs, so they can be downloaded
after a browser/server restart. Google sync always applies to the supervisor's
whole register, even though its UI task is associated with the selected project.

**Audit** shows the selected project's metadata log, LLM usage and tool outcomes.
Raw provider failures are not printed. Repository/PDF/email evidence is rendered
as text/code, not executable HTML.

## Workers, durability and shutdown

Migration 008 adds project-owned `ui_tasks`, `ui_controls` and `ui_fake_commits`.
Action payloads, upload bytes, output artifacts, monitoring flags and fake history
are durable. Browser session state holds only unsaved form/navigation state; it is
not the authoritative project store.

One runtime per database owns an OS lock, one gateway and one application worker.
Streamlit reruns reuse it. Workers never invoke Streamlit functions. Network I/O,
PDF parsing, exports, sync and delivery run off the rendering thread. Short database
edits/decisions remain synchronous. A partial unique index prevents duplicate
queued/running actions of the same kind for a project.

On restart, queued actions continue. A UI action that was running at process exit
is marked **interrupted** instead of automatically replaying an invitation, send
or remote write. Inspect the persisted subsystem state first: email attempts may
require reconciliation; Sheets pending hashes recover on the next explicit sync;
baseline drafts/gateway jobs retain their own checkpoints. Monitoring resumes
only for projects whose enabled setting was saved. No automatic send loop exists.

Use Ctrl+C to stop the server. Shutdown waits for the background worker and closes
the gateway and clients. If a forced stop interrupts an external action, the
durable task/attempt safeguards apply at restart. Do not run the standalone gateway
or another UI instance against the same database simultaneously.

Background tasks and export artifacts are retained locally; no automatic cleanup
is implemented in this phase. Task failure messages are conservative and direct
the supervisor to configuration/saved state. Provider errors may require checking
the relevant service's persisted status and setup guide. The UI is not a cloud
deployment or a multi-user permission system.

## Verification

The suite uses Streamlit `AppTest` to render every page and exercise project
creation, baseline confirmation, email approval without sending, deadline approval,
monitoring preferences and persisted downloads. Runtime tests cover durable queued
actions, non-replay of interrupted sends, upload/review restart recovery, task
deduplication and safe errors. A loopback-only headless server health check verifies
the launch entrypoint. Live services and visual browser layout remain unverified;
all integration activity in tests is fake or mocked.
