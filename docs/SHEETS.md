# Phase 7: progress register and Excel export

SupervisorAI generates one row per project from a consistent SQLite snapshot.
The existing Google spreadsheet and tab are selected in configuration. Only
columns **A–L** belong to the application. **M onward belongs to the supervisor**:
sync never writes those cells, including their headers, formulas or notes.

| Column | Generated value |
| --- | --- |
| A | Stable project ID |
| B | Student name |
| C | Registered student email |
| D | Repository owner/name |
| E | Project status |
| F | Supervisor-confirmed completed milestones / total |
| G | Earliest approved deadline among incomplete milestones |
| H | Delayed milestone count |
| I | Open or partial feedback count |
| J | Unsupported claim count |
| K | Latest non-onboarding review status and stage |
| L | Last reviewed Git SHA |

Deadline proposals do not affect the register until approved. Due dates before
today count as delayed only when there is no confirmed completion record. Dates
use the host's local date by default; the service and export accept an explicit
`today=date(...)` for reproducible reporting. No LLM is called by sync or export.

## Google setup

1. Enable the **Google Sheets API** in your Google Cloud project. Use the Desktop
   OAuth client and consent-screen test-user setup described in [EMAIL.md](EMAIL.md).
2. Set `GOOGLE_CLIENT_SECRETS_FILE` to your downloaded client JSON outside the source
   tree. Keep `TOKEN_DIR` private and outside the source tree.
3. Create/select a spreadsheet the supervisor can edit and a dedicated tab named
   `Progress` (or configure another exact title). The app does not create or resize
   spreadsheets, tabs or rows. Keep at least 12 columns and sufficient empty rows.
4. Set the spreadsheet ID, taken from its URL, and the tab title:

```dotenv
GOOGLE_SHEET_ID=
GOOGLE_SHEET_TAB=Progress
SHEETS_MAX_ROWS=5000
SHEETS_MAX_COLUMNS=256
```

5. Explicitly authorize Sheets:

```powershell
.\.venv\Scripts\python.exe -m scripts.authorize_sheets
```

This requests the `https://www.googleapis.com/auth/spreadsheets` scope and stores
a separate owner-private `sheets.json` token. It does not expand Gmail credentials.
The scope grants access to spreadsheets available to the authorized account; the
application restricts its operations to the configured ID/tab. A background call
never launches OAuth consent implicitly. Shared credential storage retains the
Windows protected DACL / POSIX 0700/0600 behavior from Phase 6.

Run a sync explicitly:

```powershell
.\.venv\Scripts\python.exe -m scripts.sync_sheets --sync
```

This command **does write the configured Google spreadsheet**. It returns project
IDs under `updated`, `unchanged`, and `conflicts`. Exit code 2 indicates pending
work or conflicts. The UI runs sync in a background worker so network
calls do not block rendering. The command validates the shared `.env`, including
mail settings; `SMTP_FROM` must be a single bare email address.

## Sync behavior and recovery

The client reads tab dimensions and cell values, including manual columns for row
occupancy checks. It uses `valueRenderOption=FORMULA` so formulas entered in generated
cells are compared as expressions rather than just their computed values. Blank
trailing cells are normalized to empty strings. Row count and tab width are bounded
by configuration; an over-limit sheet is refused, not partially processed.
[Sheets batchGet reference](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchGet)

Rows are located by project ID, not trusted row numbers. Full-row sorting is
supported. Duplicate or removed IDs produce conflicts. Do not sort only A–L while
leaving manual columns in place: that would detach your notes from their projects.
New projects append below all populated rows, including manual-only rows. Unknown
rows are preserved. An exact existing generated row can be adopted on first sync;
different untracked content is not overwritten. Headers must match the generated
schema or be empty above an otherwise empty generated area.

For each known project, the current generated cells must match either the saved
hash or a pending write hash. A mismatch reports `generated_cells_edited` and
leaves that row intact. Unaffected rows can still sync. Changed cells are sent
through **one `spreadsheets.values.batchUpdate` call per sync**, with a separate
exact `A{row}:L{row}` range for each changed row. Header initialization is included
in the same batch. `RAW` prevents student-controlled text such as names beginning
with `=` from becoming formulas. There is no append, clear, row deletion or
formatting operation. A fully unchanged register makes no write call.
[Sheets values guide](https://developers.google.com/workspace/sheets/api/guides/values)

Migration 007 preserves existing register metadata and adds the destination,
pending values/hash and safe error state. Row locations are hints, so sorting does
not violate a unique-row constraint. An OS lock serializes sync through this local
database. A pending record is committed before remote writes. If Sheets accepted
the batch but the response or local final commit was lost, the next sync compares
the remote cells with the pending hash and adopts the accepted result. If no write
occurred, the baseline still permits retry after another read. New edits conflict
instead of being blindly replayed. Provider errors are sanitized and never logged
with raw response bodies or credentials. The API client uses no automatic write
retries.

There is one register destination per local database. Once bound, changing its
ID/tab is rejected to avoid reusing hashes against an unrelated sheet. Restore
the configured destination; migrating to another register requires a deliberate
metadata migration, not an automatic reset. Legacy pre-Phase-7 register records
have no destination and are retained but refused until explicitly migrated.

Read/hash/write is **not an atomic compare-and-swap with a human editor**. Edits to
A–L made after the read but before the write can race with sync; avoid editing
generated columns while it runs. Hash checks detect cell-value changes, not
formatting changes. Manual columns remain outside every write range even during
a race. Do not run different local databases as competing writers to one tab.

## Resolving a generated-cell conflict

Keep supervisor commentary in M onward. Either restore the generated cells to
their previous values, or explicitly acknowledge their exact observed contents
before regeneration:

```python
from app.gateway.prompts import digest
from app.orchestrator.sheets import generated

# Display the conflicting row and explain that A:L will be regenerated.
observed_hash = digest(generated(observed_row))
sync.acknowledge_generated_edit(project_id, observed_hash=observed_hash)
# No remote write has occurred yet. The next explicit sync regenerates that row.
result = sync.sync()
```

Acknowledgement re-reads the row, rejects stale observations, verifies the project
and destination, and appends a supervisor decision. It is an application-level
supervisor API, never an agent tool. A missing/duplicate project ID or changed
header must be repaired in the sheet before normal sync can resume.

## Local Excel export

```powershell
.\.venv\Scripts\python.exe -m scripts.sync_sheets --excel data/progress.xlsx
```

This exports SQLite data to a **new** workbook without OAuth or Google access.
It uses the same generated fields, freezes the header, enables filtering, and
adds blank supervisor notes/decision columns. It does not import Google notes or
update an existing Excel register. Existing paths are refused, protecting any
manual edits in earlier exports. All text cells are explicitly literal strings;
formula-like student text is not executable. Text that cannot be represented
without truncation in Excel raises an error before creating the workbook.

## Offline demonstration

```powershell
.\.venv\Scripts\python.exe -m scripts.sheets_demo
.\.venv\Scripts\python.exe -m scripts.sheets_demo --excel data/fictional-register.xlsx
```

The demo ignores the `.env` file and uses a temporary database, two fictional
projects and an in-memory spreadsheet. It checks one-batch updates, manual notes,
generated-cell conflict detection and restart recovery after a lost response.
Only the optional Excel artifact persists. No live Sheets authorization or sync
was performed while developing this phase.
