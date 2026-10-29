-- All project-owned records carry project_id. Composite foreign keys prevent
-- cross-project links even when application code supplies the wrong identifier.
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    student_name TEXT NOT NULL CHECK(length(trim(student_name)) > 0),
    student_email TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_access' CHECK(status IN ('pending_access','active','paused')),
    baseline_confirmed_at TEXT,
    last_seen_sha TEXT,
    last_reviewed_sha TEXT,
    etag TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE milestones (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    completion_criteria TEXT NOT NULL,
    objectives_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(objectives_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id, id)
);

CREATE TABLE deadline_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    milestone_id TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','approved','superseded','rejected')),
    supersedes_id TEXT,
    source TEXT NOT NULL,
    decided_by TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id, milestone_id, id),
    FOREIGN KEY(project_id, milestone_id) REFERENCES milestones(project_id, id),
    FOREIGN KEY(project_id, milestone_id, supersedes_id) REFERENCES deadline_versions(project_id, milestone_id, id),
    CHECK(status NOT IN ('approved','superseded') OR decided_by IS NOT NULL)
);
CREATE UNIQUE INDEX one_approved_deadline ON deadline_versions(project_id, milestone_id) WHERE status = 'approved';

CREATE TABLE push_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    before_sha TEXT,
    after_sha TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    processed_at TEXT,
    UNIQUE(project_id, after_sha)
);
CREATE INDEX pending_pushes ON push_events(project_id, received_at) WHERE processed_at IS NULL;

CREATE TABLE cycles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    trigger TEXT NOT NULL,
    before_sha TEXT,
    after_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','pending_evidence_review','completed','failed','skipped')),
    stage TEXT NOT NULL DEFAULT 'load_memory',
    idempotency_key TEXT NOT NULL UNIQUE,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(project_id, id)
);
CREATE INDEX runnable_cycles ON cycles(project_id, status);
CREATE UNIQUE INDEX one_running_cycle ON cycles(project_id) WHERE status = 'running';

CREATE TABLE checkpoints (
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(project_id, cycle_id, stage),
    FOREIGN KEY(project_id, cycle_id) REFERENCES cycles(project_id, id)
);

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('commit','file_diff','pdf_page','email')),
    ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(meta_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id, id),
    UNIQUE(project_id, type, ref, content_hash),
    FOREIGN KEY(project_id, cycle_id) REFERENCES cycles(project_id, id)
);

CREATE TABLE reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    page_refs TEXT NOT NULL CHECK(json_valid(page_refs)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id, sha256),
    UNIQUE(project_id, version)
);

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    milestone_id TEXT,
    source_evidence_id TEXT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unverified' CHECK(status IN ('supported','unsupported','unverified')),
    UNIQUE(project_id, id),
    FOREIGN KEY(project_id, milestone_id) REFERENCES milestones(project_id, id),
    FOREIGN KEY(project_id, source_evidence_id) REFERENCES evidence(project_id, id)
);

CREATE TABLE findings (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    milestone_id TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    text TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_ids_json) AND json_type(evidence_ids_json) = 'array'),
    FOREIGN KEY(project_id, cycle_id) REFERENCES cycles(project_id, id),
    FOREIGN KEY(project_id, milestone_id) REFERENCES milestones(project_id, id)
);

CREATE TABLE feedback_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','partial','resolved')),
    opened_cycle TEXT NOT NULL,
    resolved_cycle TEXT,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_ids_json) AND json_type(evidence_ids_json) = 'array'),
    FOREIGN KEY(project_id, opened_cycle) REFERENCES cycles(project_id, id),
    FOREIGN KEY(project_id, resolved_cycle) REFERENCES cycles(project_id, id)
);
CREATE INDEX open_feedback ON feedback_items(project_id, status);

CREATE TABLE email_drafts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'followup',
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','sent','failed')),
    message_id TEXT NOT NULL UNIQUE,
    in_reply_to TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    approved_at TEXT,
    sent_at TEXT,
    error TEXT,
    send_started_at TEXT,
    delivery_uncertain INTEGER NOT NULL DEFAULT 0 CHECK(delivery_uncertain IN (0,1)),
    UNIQUE(project_id, cycle_id, kind),
    FOREIGN KEY(project_id, cycle_id) REFERENCES cycles(project_id, id),
    CHECK(status NOT IN ('approved','sent') OR approved_at IS NOT NULL),
    CHECK(status != 'sent' OR sent_at IS NOT NULL)
);

CREATE TABLE email_inbound (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    gmail_message_id TEXT NOT NULL UNIQUE,
    in_reply_to TEXT,
    references_header TEXT,
    received_at TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0 CHECK(processed IN (0,1))
);

CREATE TABLE supervisor_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE register_sync (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    sheet_row INTEGER NOT NULL UNIQUE CHECK(sheet_row >= 2),
    generated_hash TEXT,
    last_synced TEXT
);

CREATE TABLE llm_calls (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0 CHECK(tokens_in >= 0),
    tokens_out INTEGER NOT NULL DEFAULT 0 CHECK(tokens_out >= 0),
    latency_ms REAL NOT NULL DEFAULT 0 CHECK(latency_ms >= 0),
    cache_hit INTEGER NOT NULL DEFAULT 0 CHECK(cache_hit IN (0,1)),
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(project_id, cycle_id) REFERENCES cycles(project_id, id)
);
CREATE INDEX cycle_llm_calls ON llm_calls(project_id, cycle_id);

CREATE TABLE tool_calls (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(project_id, cycle_id) REFERENCES cycles(project_id, id)
);
CREATE INDEX cycle_tool_calls ON tool_calls(project_id, cycle_id, agent);

CREATE TABLE llm_cache (
    project_id TEXT NOT NULL REFERENCES projects(id),
    cache_key TEXT NOT NULL,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(project_id, cache_key)
);

CREATE TABLE usage_quota (
    project_id TEXT NOT NULL REFERENCES projects(id),
    day TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0 CHECK(tokens >= 0),
    PRIMARY KEY(project_id, day)
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id),
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX project_audit ON audit_log(project_id, id);

-- JSON arrays cannot have foreign keys; enforce their project references here.
CREATE TRIGGER findings_evidence_insert BEFORE INSERT ON findings
WHEN EXISTS (SELECT 1 FROM json_each(NEW.evidence_ids_json) j WHERE NOT EXISTS
    (SELECT 1 FROM evidence e WHERE e.project_id = NEW.project_id AND e.id = j.value))
BEGIN
    SELECT RAISE(ABORT, 'Evidence must belong to the finding project');
END;
CREATE TRIGGER findings_evidence_update BEFORE UPDATE ON findings
WHEN EXISTS (SELECT 1 FROM json_each(NEW.evidence_ids_json) j WHERE NOT EXISTS
    (SELECT 1 FROM evidence e WHERE e.project_id = NEW.project_id AND e.id = j.value))
BEGIN
    SELECT RAISE(ABORT, 'Evidence must belong to the finding project');
END;
CREATE TRIGGER feedback_evidence_insert BEFORE INSERT ON feedback_items
WHEN EXISTS (SELECT 1 FROM json_each(NEW.evidence_ids_json) j WHERE NOT EXISTS
    (SELECT 1 FROM evidence e WHERE e.project_id = NEW.project_id AND e.id = j.value))
BEGIN
    SELECT RAISE(ABORT, 'Evidence must belong to the feedback project');
END;
CREATE TRIGGER feedback_evidence_update BEFORE UPDATE ON feedback_items
WHEN EXISTS (SELECT 1 FROM json_each(NEW.evidence_ids_json) j WHERE NOT EXISTS
    (SELECT 1 FROM evidence e WHERE e.project_id = NEW.project_id AND e.id = j.value))
BEGIN
    SELECT RAISE(ABORT, 'Evidence must belong to the feedback project');
END;

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is append-only');
END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is append-only');
END;
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON supervisor_decisions
BEGIN
    SELECT RAISE(ABORT, 'Supervisor decisions are append-only');
END;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON supervisor_decisions
BEGIN
    SELECT RAISE(ABORT, 'Supervisor decisions are append-only');
END;
CREATE TRIGGER deadlines_no_delete BEFORE DELETE ON deadline_versions
BEGIN
    SELECT RAISE(ABORT, 'Deadline history cannot be deleted');
END;
CREATE TRIGGER deadlines_immutable_values BEFORE UPDATE ON deadline_versions
WHEN NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id
    OR NEW.milestone_id IS NOT OLD.milestone_id OR NEW.due_date IS NOT OLD.due_date
    OR NEW.supersedes_id IS NOT OLD.supersedes_id OR NEW.source IS NOT OLD.source
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'Create a deadline version instead of editing history');
END;
