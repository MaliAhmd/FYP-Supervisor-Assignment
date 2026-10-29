CREATE TABLE github_state (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    tracked_branch TEXT,
    next_poll_at REAL NOT NULL DEFAULT 0,
    last_polled_at REAL,
    last_push_at REAL,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    blocked_until REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE github_invitations (
    project_id TEXT NOT NULL REFERENCES projects(id),
    invitation_id INTEGER NOT NULL,
    permissions TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','unavailable')),
    discovered_at TEXT NOT NULL,
    accepted_at TEXT,
    PRIMARY KEY(project_id, invitation_id)
);
ALTER TABLE push_events ADD COLUMN observed_at REAL NOT NULL DEFAULT 0;
ALTER TABLE push_events ADD COLUMN source TEXT NOT NULL DEFAULT 'poll';
CREATE INDEX pending_push_order ON push_events(project_id, observed_at) WHERE processed_at IS NULL;

-- Preserve pending events if upgrading a database populated before this trigger.
UPDATE push_events SET observed_at=coalesce(CAST(strftime('%s',received_at) AS REAL),0) WHERE observed_at=0;
INSERT INTO github_state(project_id,last_push_at)
SELECT p.id,(SELECT max(e.observed_at) FROM push_events e WHERE e.project_id=p.id)
FROM projects p;
