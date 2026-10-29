"""Project-scoped persistence; no caller-supplied SQL identifiers or fragments."""

from collections.abc import Mapping
from hashlib import sha256
import json
import re
from uuid import uuid4

from app.config import validate_email
from app.db.database import Database
from app.logging_config import redact


def new_id() -> str:
    return uuid4().hex


class ProjectNotFound(LookupError):
    pass


class ProjectStore:
    def __init__(self, db: Database, project_id: str):
        self.db = db
        self.project_id = project_id
        self.project()

    @classmethod
    def create(cls, db: Database, student_name: str, student_email: str, repo_full_name: str):
        name = student_name.strip()
        if not name:
            raise ValueError("Student name is required")
        address = validate_email(student_email)
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+", repo_full_name):
            raise ValueError("Repository must use owner/repo format")
        project_id = new_id()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO projects(id,student_name,student_email,repo_full_name) VALUES (?,?,?,?)",
                (project_id, name, address, repo_full_name),
            )
            conn.execute(
                "INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,?,?,?)",
                (project_id, "project.created", "supervisor", "{}"),
            )
        return cls(db, project_id)

    @staticmethod
    def list_projects(db: Database) -> list[dict]:
        """Explicit supervisor-level enumeration; never pass this to an agent."""
        with db.transaction(write=False) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY created_at,id")]

    def project(self) -> dict:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (self.project_id,)).fetchone()
        if row is None:
            raise ProjectNotFound(self.project_id)
        return dict(row)

    def audit(self, event: str, metadata: Mapping | None = None, *, actor: str = "application") -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.]{0,99}", event):
            raise ValueError("Audit event must be a metadata event name")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,?,?,?)",
                (self.project_id, event, actor, json.dumps(redact(metadata or {}), default=str)),
            )

    def audit_history(self) -> list[dict]:
        with self.db.transaction(write=False) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM audit_log WHERE project_id=? ORDER BY id", (self.project_id,),
            )]

    def create_cycle(self, after_sha: str, *, trigger: str = "manual", before_sha: str | None = None) -> str:
        key = sha256(json.dumps([self.project_id, after_sha]).encode()).hexdigest()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO cycles(id,project_id,trigger,before_sha,after_sha,idempotency_key)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING""",
                (new_id(), self.project_id, trigger, before_sha, after_sha, key),
            )
            return conn.execute(
                "SELECT id FROM cycles WHERE project_id=? AND idempotency_key=?",
                (self.project_id, key),
            ).fetchone()["id"]

    def save_checkpoint(self, cycle_id: str, stage: str, payload: Mapping) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO checkpoints(project_id,cycle_id,stage,payload) VALUES (?,?,?,?)
                   ON CONFLICT(project_id,cycle_id,stage) DO UPDATE SET payload=excluded.payload""",
                (self.project_id, cycle_id, stage, json.dumps(payload)),
            )
            conn.execute(
                "UPDATE cycles SET stage=? WHERE project_id=? AND id=?",
                (stage, self.project_id, cycle_id),
            )

    def checkpoint(self, cycle_id: str, stage: str) -> dict | None:
        with self.db.transaction(write=False) as conn:
            row = conn.execute(
                "SELECT payload FROM checkpoints WHERE project_id=? AND cycle_id=? AND stage=?",
                (self.project_id, cycle_id, stage),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def add_evidence(self, cycle_id: str, kind: str, ref: str, content: str, meta: Mapping | None = None) -> str:
        digest = sha256(content.encode()).hexdigest()
        with self.db.transaction() as conn:
            # Check ownership even when the evidence itself already exists.
            if conn.execute("SELECT id FROM cycles WHERE project_id=? AND id=?", (self.project_id, cycle_id)).fetchone() is None:
                raise ValueError("Cycle does not belong to this project")
            conn.execute(
                """INSERT INTO evidence(id,project_id,cycle_id,type,ref,content_hash,content,meta_json)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project_id,type,ref,content_hash) DO NOTHING""",
                (new_id(), self.project_id, cycle_id, kind, ref, digest, content, json.dumps(meta or {})),
            )
            return conn.execute(
                "SELECT id FROM evidence WHERE project_id=? AND type=? AND ref=? AND content_hash=?",
                (self.project_id, kind, ref, digest),
            ).fetchone()["id"]

    def evidence(self, evidence_id: str) -> dict | None:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM evidence WHERE project_id=? AND id=?", (self.project_id, evidence_id)).fetchone()
        return dict(row) if row else None

    def cache_put(self, key: str, model: str, response: Mapping) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO llm_cache(project_id,cache_key,model,response_json) VALUES (?,?,?,?)
                   ON CONFLICT(project_id,cache_key) DO UPDATE SET model=excluded.model,response_json=excluded.response_json""",
                (self.project_id, key, model, json.dumps(response)),
            )

    def cache_get(self, key: str) -> dict | None:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT response_json FROM llm_cache WHERE project_id=? AND cache_key=?", (self.project_id, key)).fetchone()
        return json.loads(row["response_json"]) if row else None
