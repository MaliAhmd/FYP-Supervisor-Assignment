ALTER TABLE email_drafts ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE email_drafts ADD COLUMN approval_hash TEXT;
ALTER TABLE email_drafts ADD COLUMN actual_to TEXT;
ALTER TABLE email_drafts ADD COLUMN delivery_mode TEXT;
ALTER TABLE email_drafts ADD COLUMN provider_message_id TEXT;
CREATE TABLE email_attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    actual_to TEXT NOT NULL,
    mode TEXT NOT NULL,
    transport TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('started','sent','failed','uncertain','confirmed_sent','confirmed_not_sent')),
    error TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    FOREIGN KEY(project_id,draft_id) REFERENCES email_drafts(project_id,id)
);
CREATE UNIQUE INDEX email_draft_project_id ON email_drafts(project_id,id);
CREATE TABLE gmail_poll_state (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    watermark INTEGER NOT NULL DEFAULT 0,
    scan_start INTEGER,
    scan_end INTEGER,
    scan_query TEXT,
    page_token TEXT,
    next_poll REAL NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
ALTER TABLE email_inbound ADD COLUMN sender TEXT;
ALTER TABLE email_inbound ADD COLUMN message_id TEXT;
ALTER TABLE email_inbound ADD COLUMN thread_id TEXT;
