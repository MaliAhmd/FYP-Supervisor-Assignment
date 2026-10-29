-- Row numbers are location hints, not identity: users may sort the register.
-- Preserve legacy records; an unbound legacy destination requires review.
CREATE TABLE register_sync_new (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    sheet_row INTEGER NOT NULL CHECK(sheet_row>=2),
    generated_hash TEXT,
    last_synced TEXT,
    spreadsheet_id TEXT NOT NULL DEFAULT '',
    tab_name TEXT NOT NULL DEFAULT '',
    pending_hash TEXT,
    pending_values TEXT CHECK(pending_values IS NULL OR json_valid(pending_values)),
    last_error TEXT
);
INSERT INTO register_sync_new(project_id,sheet_row,generated_hash,last_synced)
    SELECT project_id,sheet_row,generated_hash,last_synced FROM register_sync;
DROP TABLE register_sync;
ALTER TABLE register_sync_new RENAME TO register_sync;
