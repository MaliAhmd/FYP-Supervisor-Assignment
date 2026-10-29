CREATE TABLE ui_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload)),
    input_blob BLOB,
    output_blob BLOB,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed','interrupted')),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);
CREATE UNIQUE INDEX one_pending_ui_action ON ui_tasks(project_id,kind) WHERE status IN ('queued','running');
CREATE TABLE ui_controls (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    monitoring INTEGER NOT NULL DEFAULT 0 CHECK(monitoring IN (0,1)),
    replies_enabled INTEGER NOT NULL DEFAULT 0 CHECK(replies_enabled IN (0,1)),
    batch_id TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE ui_fake_commits (
    project_id TEXT NOT NULL REFERENCES projects(id),
    sequence INTEGER NOT NULL,
    files_json TEXT NOT NULL CHECK(json_valid(files_json)),
    PRIMARY KEY(project_id,sequence)
);
