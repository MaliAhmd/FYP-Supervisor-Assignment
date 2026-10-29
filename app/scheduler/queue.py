"""Transport-neutral push ingestion and durable, trailing-edge debounce."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import time
from uuid import uuid4

from app.db import Database
from app.integrations.github.models import sha_value


def timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


class ReviewQueue:
    def __init__(self, db: Database, debounce_seconds: int = 300, *, clock=time.time):
        self.db, self.debounce_seconds, self.clock = db, debounce_seconds, clock

    def observe_head(self, project_id: str, after_sha: str, *, source: str = "poll", etag: str | None = None,
                     lease_owner: str | None = None) -> bool:
        """Trusted triggers pass a verified current head, never an unchecked webhook payload.

        Duplicate SHAs do not create events or reset debounce unless the observed
        head actually changed (including a branch reverting to an older SHA).
        """
        if source not in {"poll", "webhook", "manual"}:
            raise ValueError("Unknown trigger source")
        after_sha, now = sha_value(after_sha), self.clock()
        with self.db.transaction() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise LookupError("Unknown project")
            if project["status"] != "active" or not project["baseline_confirmed_at"]:
                return False
            conn.execute("INSERT INTO github_state(project_id) VALUES (?) ON CONFLICT DO NOTHING", (project_id,))
            state = conn.execute("SELECT * FROM github_state WHERE project_id=?", (project_id,)).fetchone()
            if lease_owner is not None and (state["lease_owner"] != lease_owner or state["lease_until"] <= now):
                return False
            if project["last_seen_sha"] == after_sha:
                conn.execute("UPDATE projects SET etag=? WHERE id=?", (etag, project_id))
                return False
            event_id = uuid4().hex
            changed = conn.execute("""INSERT INTO push_events(id,project_id,before_sha,after_sha,received_at,observed_at,source)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id,after_sha) DO NOTHING""",
                                  (event_id, project_id, project["last_seen_sha"], after_sha, timestamp(now), now, source)).rowcount > 0
            conn.execute("UPDATE projects SET last_seen_sha=?,etag=? WHERE id=?", (after_sha, etag, project_id))
            conn.execute("UPDATE github_state SET last_push_at=? WHERE project_id=?", (now, project_id))
            if changed:
                conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'github.head_observed','application',?)",
                             (project_id, json.dumps({"after_sha": after_sha, "source": source})))
            return changed

    def flush(self, project_id: str) -> str | None:
        """Atomically consume a mature burst and enqueue at most one review cycle."""
        now = self.clock()
        with self.db.transaction() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise LookupError("Unknown project")
            state = conn.execute("SELECT * FROM github_state WHERE project_id=?", (project_id,)).fetchone()
            if (project["status"] != "active" or not project["baseline_confirmed_at"] or state is None
                    or state["last_push_at"] is None or state["last_push_at"] + self.debounce_seconds > now):
                return None
            pending = conn.execute("SELECT id FROM push_events WHERE project_id=? AND processed_at IS NULL", (project_id,)).fetchall()
            if not pending:
                return None
            if conn.execute("SELECT 1 FROM cycles WHERE project_id=? AND status IN ('queued','running','pending_evidence_review') LIMIT 1", (project_id,)).fetchone():
                return None
            after = project["last_seen_sha"]
            key = sha256(json.dumps([project_id, after]).encode()).hexdigest()
            existing = conn.execute("SELECT id FROM cycles WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()
            cycle_id = None
            if after != project["last_reviewed_sha"] and existing is None:
                cycle_id = uuid4().hex
                conn.execute("""INSERT INTO cycles(id,project_id,trigger,before_sha,after_sha,idempotency_key)
                    VALUES (?,?,'push',?,?,?)""", (cycle_id, project_id, project["last_reviewed_sha"], after, key))
                conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'review.enqueued','application',?)",
                             (project_id, json.dumps({"cycle_id": cycle_id, "merged_events": len(pending)})))
            conn.execute("UPDATE push_events SET processed_at=? WHERE project_id=? AND processed_at IS NULL", (timestamp(now), project_id))
            return cycle_id

    def pending_cycles(self, project_id: str) -> list[dict]:
        with self.db.transaction(write=False) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM cycles WHERE project_id=? AND status IN ('queued','running','pending_evidence_review') ORDER BY rowid", (project_id,))]
