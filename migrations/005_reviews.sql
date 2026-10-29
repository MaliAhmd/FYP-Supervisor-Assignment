CREATE TABLE review_state (
    project_id TEXT NOT NULL REFERENCES projects(id),
    cycle_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload)),
    reason TEXT,
    PRIMARY KEY(project_id,cycle_id),
    FOREIGN KEY(project_id,cycle_id) REFERENCES cycles(project_id,id)
);
CREATE TABLE milestone_completions (
    project_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(project_id,milestone_id),
    FOREIGN KEY(project_id,milestone_id) REFERENCES milestones(project_id,id),
    FOREIGN KEY(project_id,evidence_id) REFERENCES evidence(project_id,id)
);
CREATE TABLE claim_verifications (
    project_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json)),
    reason TEXT NOT NULL,
    PRIMARY KEY(project_id,claim_id),
    FOREIGN KEY(project_id,claim_id) REFERENCES claims(project_id,id),
    FOREIGN KEY(project_id,cycle_id) REFERENCES cycles(project_id,id)
);
CREATE TABLE reviewed_documents (
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    PRIMARY KEY(project_id,document_id),
    FOREIGN KEY(project_id,document_id) REFERENCES pdf_documents(project_id,id),
    FOREIGN KEY(project_id,cycle_id) REFERENCES cycles(project_id,id)
);
