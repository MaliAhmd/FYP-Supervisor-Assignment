ALTER TABLE llm_calls ADD COLUMN batch_id TEXT;
ALTER TABLE llm_calls ADD COLUMN quota_day TEXT;
ALTER TABLE llm_calls ADD COLUMN charged_tokens INTEGER NOT NULL DEFAULT 0 CHECK(charged_tokens >= 0);
ALTER TABLE llm_calls ADD COLUMN http_status INTEGER;
ALTER TABLE llm_calls ADD COLUMN attempted INTEGER NOT NULL DEFAULT 0 CHECK(attempted IN (0,1));
CREATE INDEX batch_llm_calls ON llm_calls(batch_id, cache_hit);

-- Payloads contain project data, never credentials. Specs are supplied by trusted
-- application code and fingerprinted; incompatible jobs wait for manual review.
CREATE TABLE gateway_jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    request_key TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','pending','failed')),
    available_at REAL NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    repair_count INTEGER NOT NULL DEFAULT 0,
    response_json TEXT CHECK(response_json IS NULL OR json_valid(response_json)),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id, request_key),
    FOREIGN KEY(project_id, cycle_id) REFERENCES cycles(project_id, id)
);
CREATE INDEX ready_gateway_jobs ON gateway_jobs(status, available_at, project_id, seq);

-- Latest observations per project/model, aggregated only by the shared limiter.
CREATE TABLE gateway_model_limits (
    project_id TEXT NOT NULL REFERENCES projects(id),
    model TEXT NOT NULL,
    observed_at REAL NOT NULL,
    headers_json TEXT NOT NULL CHECK(json_valid(headers_json)),
    blocked_until REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(project_id, model)
);
