# Phase 9: three projects, three review rounds, full restart

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_demo
```

The script creates a new, isolated `data/phase9_demo` directory. It ignores both
`.env` and integration-related environment variables: every setting starts from
an explicit safe default, and every provider is an injected fake/recording client.
No real GitHub invitation, SMTP message, Gmail read or Google Sheets write occurs.
It does not seed or modify your configured production database.

Each project uses a real generated proposal PDF, one fake baseline extraction,
explicit fixture supervisor confirmation and a simulated invitation acceptance.
There are two milestones per project. The demo date is **2026-10-26**, including
review memory, delay calculations and the exported register; outcomes do not
depend on the host's current calendar date.

| Fictional project | Round 1 | Round 2 | Round 3 after restart |
| --- | --- | --- | --- |
| Amina | Code review, open feedback | New code, partial feedback | New code, feedback resolved; overdue milestone still flagged |
| Bilal | Code review and follow-up draft | Recorded fake delivery, matched reply, unsupported completion claim, proposed deadline explicitly approved | New code review loads the saved claim, approved deadline and prior feedback |
| Chen | Code review, open feedback | First report PDF, relevant page-2 citation | Revised PDF with a new hash/version and new page evidence |

Amina's prototype deadline is 2026-10-01 and there is no confirmed completion
evidence, so it remains delayed. Bilal's reply claims completion without independent
evidence; deterministic verification marks the claim unsupported. Its requested
2026-11-30 date remains a proposal until the scripted supervisor decision approves
it, retaining the superseded 2026-10-01 version. A report revision alone does not
confirm milestone completion; Chen also remains delayed on the prototype.

The fake model outputs are trusted scenario fixtures, not evidence of live-model
accuracy. The controller, schemas, citation checks, deadlines, queue, gateway,
budgets, SQLite transactions, PDF parsing/retrieval, approvals, reply matcher and
register services are the actual application implementations. The demo engine
only supplies a fixed calendar date to its memory/drafting inputs. No student code
is executed.

## Real process restart

The default command completes round two, closes the gateway, writes a manifest,
then invokes a **new Python interpreter** to finish round three. The new process
reopens SQLite and reconstructs only fake Git history from the manifest. It does
not upload/extract proposals again. Checks verify that round three uses the saved
reviewed SHA, two saved milestones, feedback, earlier claim and approved deadline.

To demonstrate a full stop with separate terminal commands:

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory data/demo-manual --stop-after-2
# The first process has exited. Start another process:
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory data/demo-manual --resume
```

Only recognized completed-round-two manifests may resume. Do not edit the database
or run UI workers against it while demonstrating this checkpoint; resume checks
saved counts before proceeding. The seed driver deliberately refuses unexpected
partial runs instead of guessing how to reseed them. For a failure outside the
declared checkpoint, use a new directory or inspect the underlying service state;
arbitrary seed-driver crash repair is not implemented.

A completed demo is safe to verify again:

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_demo --directory data/phase9_demo --resume
```

This verifies saved results without repeating reviews, provider attempts, drafts,
or exports. A fresh seed refuses any existing destination, including unrelated
files. Use another new directory for another run; the script never deletes one.

## Outputs and checks

The directory contains:

- `supervisorai.sqlite3`: 3 projects, 6 milestones, **9 completed review cycles**
  plus 3 completed onboarding cycles, 9 email drafts, 8 findings, 1 unsupported
  claim, two report versions with page evidence, and retained deadline history.
- `manifest.json`: scenario identity, stable project/cycle IDs, fake commit history,
  checkpoint counts and separate seed/resume process IDs. It contains no credentials.
- `summary.json`: checked scenario outcomes, provider-attempt/cache counts and a
  Boolean confirming a different resume process.
- `progress.xlsx`: a standalone register snapshot with literal text and blank
  supervisor columns. It contains fictional data only.

With the current fixed fixtures, 30 LLM log records consist of 28 fake provider
attempts and two cache hits. All calls retain the normal 3,000 input cap, 700 output
cap, six-attempt cycle budget, two-attempt coordinator budget, six-tool specialist
budget and 25-attempt shared batch budget. Each round has a shared explicit batch
ID. Only local fake rate pacing is raised to avoid real waiting; PDF/parser and
per-call/cycle/batch limits are not bypassed. Fake responses report fixture usage,
not measured live-provider token usage.

Git push cycles exercise the default five-minute debounce using a simulated clock,
not a shortened configuration or a five-minute sleep. Re-observing a head and
re-driving completed cycles must not create duplicates. Report duplicate uploads
reuse the same document; revised bytes produce a second version. Reply ingestion
is called twice to verify deduplication. The one approved fixture email is sent
twice through the application but reaches the recording sender only once.

The final in-memory Sheets sync uses one initial batch, preserves a manual note,
and performs no write for unchanged data. Excel export uses the production export
implementation. All scenario requirements are checked before reporting success.

## Running equivalent scenarios on real test repositories

The seed command intentionally has no live switch. Use a **separate application
database**, controlled test accounts/repos, and the normal UI for a live evaluation:

1. Configure GitHub read access using [GITHUB.md](GITHUB.md). Set `GITHUB_MODE=live`.
   For meaningful model output, set `LLM_MODE=live` with your configured Groq models
   and key; staying in fake LLM mode exercises plumbing only.
2. Keep `EMAIL_MODE=test` and set your own controlled `TEST_REDIRECT_EMAIL`.
   Configure SMTP/Gmail using [EMAIL.md](EMAIL.md). Test mode sends actual messages
   to the redirect mailbox; it is not an offline simulator. Use a separate test
   spreadsheet if checking [Sheets sync](SHEETS.md).
3. Start the [UI](UI.md). Add three fictional students with three distinct test
   repositories and small proposal PDFs containing two explicit criteria/dates.
   Set one prototype deadline before today. Inspect and confirm each baseline.
   Have the repository owners invite the supervisor and accept through the UI.
4. For round one, commit a small source file in each repository using the owner
   account outside SupervisorAI. Enable monitoring with a shared batch ID, wait
   for debounce or choose **Review now**, and inspect findings and pending drafts.
   SupervisorAI never pushes source changes itself.
5. For round two, make another small source change for project A. For B, edit and
   explicitly approve/send its follow-up to your controlled redirect, then reply
   in that mail thread with an unsupported completion claim and a concrete extension
   request. Poll matching replies and inspect the unsupported/unverified evidence
   result. Approve only the intended proposed deadline. For C, upload a short report
   whose second page addresses the saved criteria; inspect its page citation.
6. Stop Streamlit after those reviews finish. Restart with the same database and
   no proposal re-upload. Make a third code change for A and B and upload a revised
   report for C. Use an explicit round-three batch ID and process the queued reviews.
7. Verify the reviewed-SHA bases, saved milestones, deadline version history,
   earlier feedback/claim memory, report hashes/page refs, pending drafts and audit
   usage. Repeat an unchanged poll/upload to check deduplication. Confirm the register
   leaves manual columns intact. Never treat a model's valid citation alone as proof
   of completion.

Live outputs are not deterministic. In particular, a model may omit a deadline
proposal, return invalid citations or exceed a budget. Inspect the pending reason
and evidence; do not edit the database to force a passing result. Small changes,
short criteria and concise PDFs are important under the conservative input cap.
This live procedure is documented but has not been performed during development.
