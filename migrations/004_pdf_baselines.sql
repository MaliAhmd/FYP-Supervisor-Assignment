CREATE UNIQUE INDEX reports_project_id ON reports(project_id,id);
CREATE UNIQUE INDEX gateway_jobs_project_id ON gateway_jobs(project_id,id);

CREATE TABLE pdf_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('proposal','report')),
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    pdf_bytes BLOB NOT NULL,
    page_count INTEGER NOT NULL CHECK(page_count > 0),
    text_chars INTEGER NOT NULL CHECK(text_chars >= 0),
    status TEXT NOT NULL CHECK(status IN ('ready','no_text')),
    report_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id,id),
    UNIQUE(project_id,kind,sha256),
    FOREIGN KEY(project_id,cycle_id) REFERENCES cycles(project_id,id),
    FOREIGN KEY(project_id,report_id) REFERENCES reports(project_id,id)
);

CREATE TABLE pdf_pages (
    project_id TEXT NOT NULL REFERENCES projects(id),
    document_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK(page_number > 0),
    evidence_id TEXT NOT NULL,
    PRIMARY KEY(project_id,document_id,page_number),
    FOREIGN KEY(project_id,document_id) REFERENCES pdf_documents(project_id,id),
    FOREIGN KEY(project_id,evidence_id) REFERENCES evidence(project_id,id)
);

CREATE TABLE baseline_drafts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    document_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    job_id TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded' CHECK(status IN ('uploaded','extracting','editable','pending','confirmed','superseded')),
    draft_json TEXT NOT NULL DEFAULT '{"objectives":[],"milestones":[],"warnings":[]}' CHECK(json_valid(draft_json)),
    context_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(context_json)),
    revision INTEGER NOT NULL DEFAULT 0,
    limited_coverage INTEGER NOT NULL DEFAULT 1 CHECK(limited_coverage IN (0,1)),
    reason TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id,id),
    UNIQUE(project_id,document_id),
    FOREIGN KEY(project_id,document_id) REFERENCES pdf_documents(project_id,id),
    FOREIGN KEY(project_id,cycle_id) REFERENCES cycles(project_id,id),
    FOREIGN KEY(project_id,job_id) REFERENCES gateway_jobs(project_id,id)
);
CREATE UNIQUE INDEX one_confirmed_baseline ON baseline_drafts(project_id) WHERE status='confirmed';

CREATE TABLE project_objectives (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    baseline_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    text TEXT NOT NULL,
    UNIQUE(project_id,ordinal),
    FOREIGN KEY(project_id,baseline_id) REFERENCES baseline_drafts(project_id,id)
);

CREATE TRIGGER confirmed_baseline_no_update BEFORE UPDATE ON baseline_drafts
WHEN OLD.status='confirmed'
BEGIN
    SELECT RAISE(ABORT,'Confirmed baseline is immutable');
END;
CREATE TRIGGER confirmed_baseline_no_delete BEFORE DELETE ON baseline_drafts
WHEN OLD.status='confirmed'
BEGIN
    SELECT RAISE(ABORT,'Confirmed baseline is immutable');
END;
