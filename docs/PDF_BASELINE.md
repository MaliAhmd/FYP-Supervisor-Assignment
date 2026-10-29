# Phase 4: PDFs and confirmed baselines

## Run the offline demo

```powershell
.\.venv\Scripts\python.exe -m scripts.baseline_demo
```

This creates a temporary database and a fictional proposal PDF, uses one scripted
fake LLM response, edits a deadline, confirms the baseline, reopens the database,
and checks saved objectives/milestones without re-uploading the proposal. It also
deduplicates an unchanged report, stores a report revision, and retrieves relevant
page evidence. No external service is contacted and no email is sent.

To retain the demo's records, use `--database data/baseline_demo.sqlite3`. Each run
with that option adds a fictional project. Demo dates and content are fixtures;
production code contains no project-specific objectives, milestones, or deadlines.

## PDF ingestion and persistence

`PDFStore.ingest(project_id, content, kind="proposal"|"report", filename=...)`
accepts PDF bytes. It stores the original bytes, SHA-256, page text/evidence,
one-based page numbers, and safe display filename in SQLite. Upload filenames are
never used as filesystem destinations. An optional `cycle_id` must belong to the
same project; otherwise an upload cycle is created atomically with the document.

Repeated uploads of the same hash and kind return the existing document. The shared
store serializes ingestion; SQLite constraints also prevent duplicate records and
report versions across store instances. If a PDF already exists under the other
kind, its extracted pages are reused. A new report hash creates a monotonically
increasing version for that project, retaining previous versions and page references.
Report `path` values use `sqlite:pdf_documents/<id>`; call `PDFStore.bytes` to retrieve
the bytes rather than treating that identifier as a local path.

Page evidence has type `pdf_page`, a stable evidence ID, text hash, document hash,
and a reference such as `pdf:<document_id>#page=2`. All document, page, evidence,
report, and draft reads are project-scoped. Report ingestion does not modify an
approved baseline. A newly uploaded report leaves review work queued for Phase 5;
unchanged reports create neither a new version nor a new upload cycle.
Standalone upload cycles use `proposal_upload`/`report_upload` triggers and
`proposal:<hash>`/`report:<hash>` markers, not Git commit SHAs. The Phase 5
orchestrator must route these separately from push reviews. When a PDF comes from
an existing repository-review cycle, pass that cycle's ID to ingestion instead.

## Parsing limits and limitations

The parser runs in a disposable subprocess with a timeout, no application
credentials in its environment, and no visible Windows console. It uses pypdf's
strict reader and configurable stream-decompression limits. Parser messages and
exceptions are reduced to safe error codes; PDF text is not logged. Pages, upload
size, total extracted text, and content streams are bounded. Exceeding a limit
rejects the document instead of silently saving partial page extraction.

| Setting | Default |
| --- | --- |
| `MAX_UPLOAD_BYTES` | 20 MiB |
| `PDF_MAX_PAGES` | 200 |
| `PDF_MAX_TEXT_CHARS` | 200,000 |
| `PDF_MAX_STREAM_BYTES` | 8 MiB |
| `PDF_TIMEOUT_SECONDS` | 30 |
| `PDF_PASSAGE_CHARS` | 600 |
| `REPORT_TOP_K` | 4 |

The minimum pypdf version is now 6.21.2, matching the tested configuration API.
The subprocess and parsing limits are resource controls, not a security sandbox
or a hard OS-wide memory quota. PDFs are parsed as data; embedded instructions,
links, attachments, and JavaScript are not executed by this flow.

Encrypted, malformed, oversized, and empty documents are rejected. A PDF containing
pages but no extractable text is saved as `no_text`; proposal extraction makes no
LLM call. Supply an OCR-processed/text PDF or enter a baseline manually. Mixed
documents retain empty page numbers and flag limited extraction coverage.

pypdf does not perform OCR, and text extraction can lose reading order or layout.
Text coverage does not imply that figures, images, or tables were understood.
The supervisor must inspect the proposal before confirming. See the official
[pypdf text-extraction guidance](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).

## Retrieval

`Retriever(store).retrieve(project_id, document_id, milestone_criteria)` applies
deterministic BM25 to bounded passages, with no embeddings or LLM. Results contain
text, page number, page evidence ID, stable reference, character offsets, and score.
The default is at most four positive matches; no keyword match returns an empty
list. An empty query returns the first passages in page order. Equal scores use
page/offset order, so results are deterministic.

Evidence IDs identify the full persisted page; passage offsets identify the exact
excerpt. The later report reviewer can cite those pages while deterministic
verification checks the project's evidence ownership. Retrieval itself does not
assert milestone completion.

## Onboarding API

Register `baseline_spec()` in the application's shared gateway alongside other
agent specifications. It supplies a dedicated prompt file, Pydantic schema, fast
model tier, and an empty tool allowlist. The gateway adds its runtime schema,
remaining budgets, and untrusted-data delimiters.

```python
from app.agents.baseline import baseline_spec
from app.config import Settings
from app.db import Database, ProjectStore
from app.gateway import Gateway
from app.integrations.pdf.store import PDFStore
from app.orchestrator.baseline import BaselineService

settings = Settings()
db = Database(settings.database_path)
db.migrate()
project = ProjectStore.create(db, "Student Name", "student@example.com", "owner/repo")
pdfs = PDFStore(db, settings)
with Gateway(db, settings, [baseline_spec()]) as gateway:
    onboarding = BaselineService(db, pdfs, gateway)
    # uploaded_bytes comes from the future UI's PDF uploader.
    draft = onboarding.upload(project.project_id, uploaded_bytes, filename="proposal.pdf")
    draft = onboarding.request_extraction(project.project_id, draft["id"], "onboarding-batch-1")
    # Submit returns immediately after queuing. Poll/collect later in the UI.
    draft = onboarding.collect(project.project_id, draft["id"])
```

PDF parsing is synchronous at the service boundary and should be invoked by the
UI's background worker in Phase 8. The LLM gateway already runs in a background
thread; scripts can call `gateway.wait` before `collect`. There is no Streamlit
wizard yet and no state depends on `st.session_state`.

The states are `uploaded`, `extracting`, `editable`, `pending`, `confirmed`, and
`superseded`. Queue insertion and the draft's job ID/status are committed together.
After restart, call `drafts(project_id)` and then `collect` for pending extraction
jobs. Draft listing also recovers an upload committed just before the process
stopped prior to creating its draft record. A restarted gateway executes its
persisted jobs under the original budgets.

## One extraction and visible coverage

One logical extraction job gathers objectives and milestones in one response.
There are no per-page LLM calls. The gateway's existing bounded transport retries
and at most one JSON regeneration still apply; each attempt consumes budget.
Repeated extraction requests for the same draft reuse its state instead of
overwriting supervisor edits or scheduling another call.

The context builder prioritizes objective/milestone/schedule passages and fits them
within the complete input budget, including the schema and repair envelope. It
records selected page/character ranges, total/included text counts, and blank pages.
Long proposals may have omitted text. This is surfaced through `limited_coverage`
and the persisted context; confirming such a draft requires
`acknowledge_limited_coverage=True` after the supervisor reviews the omitted content.
Manual baselines entered before extraction likewise require that acknowledgement.

The model is instructed to leave missing dates as null, leave absent requirements
empty, and cite source page numbers. Dates must use valid `YYYY-MM-DD` values.
An invented/out-of-context page number produces a pending draft rather than an
approved baseline. A valid page number only establishes that the cited page was
provided; it does not prove the semantic accuracy of the extraction.

The default fake client deliberately returns an empty editable draft with a manual
entry notice. It does not infer requirements or dates from arbitrary PDFs. Tests
and the demo explicitly provide schema-specific fake fixtures. No live Groq PDF
extraction has been performed.

## Editing and confirmation

Pass the returned revision to prevent stale UI edits:

```python
edited = onboarding.edit(project_id, draft_id, edited_draft,
                         expected_revision=current_revision)
baseline = onboarding.confirm(project_id, draft_id,
                              expected_revision=edited["revision"],
                              acknowledge_limited_coverage=reviewed_omitted_content)
```

Only trusted application code responding to a supervisor action calls `edit` or
`confirm`; neither is an agent tool. A running extraction must finish before it
can be manually replaced. Queued/pending extraction can be cancelled by an explicit
edit. Confirmation requires objectives, distinct milestone titles, completion
criteria, and every deadline. No missing date is silently invented.

Confirmation atomically writes project objectives, milestones, initial approved
deadline versions, an append-only supervisor decision, the confirmation timestamp,
and a completed onboarding cycle. It preserves project access/paused status.
Abandoned proposal drafts/cycles are superseded/skipped so they cannot block later
push reviews. Duplicate confirmation is idempotent, including concurrent clicks.

The confirmed draft is immutable. Replacing a proposal cannot overwrite it;
subsequent deadline changes use the versioned approval workflow added in Phase 5.
`BaselineService.baseline(project_id)` reads authoritative objectives, criteria,
and current approved dates directly from SQLite. It needs no PDF parsing, gateway,
API key, or original upload after restart. The source bytes/pages remain available
for provenance; ordinary reviews will use the saved baseline.

If extraction is pending because of quota, the application can resume the saved
gateway job when quota permits, or the supervisor can enter a baseline manually.
No-text and input-cap failures save a baseline checkpoint without invoking the LLM.

## Validation scope

Tests use real generated PDF files and the real pypdf parser for page extraction,
encryption/limit failures, versions, and retrieval. Fake/mocked LLM tests cover
bounded extraction, injection delimiters, page references, persistent jobs,
concurrent submission/confirmation, optimistic editing, rollback, project isolation,
and restart without re-upload. Migration `004_pdf_baselines.sql` preserves existing
project/report records and can attach stored reports to newly ingested page evidence.
