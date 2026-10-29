"""Polling trigger only: no LLM calls, review execution, or outbound messages."""

import json
import threading
import time
from typing import Protocol
from uuid import uuid4

from app.config import Settings
from app.db import Database
from app.integrations.github.models import GitHubError, GitHubReader
from app.scheduler.queue import ReviewQueue, timestamp


class PushTrigger(Protocol):
    def tick(self) -> list[dict]: ...


class PollingTrigger:
    def __init__(self, db: Database, settings: Settings, reader: GitHubReader, *, clock=time.time):
        self.db, self.settings, self.reader, self.clock = db, settings, reader, clock
        self.queue = ReviewQueue(db, settings.debounce_seconds, clock=clock)
        self._lock = threading.Lock()

    def poll_project(self, project_id: str) -> str:
        now, owner = self.clock(), uuid4().hex
        with self.db.transaction() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise LookupError("Unknown project")
            if project["status"] != "active" or not project["baseline_confirmed_at"]:
                return "inactive"
            conn.execute("INSERT INTO github_state(project_id) VALUES (?) ON CONFLICT DO NOTHING", (project_id,))
            # All repos currently share one credential, so a rate limit is global.
            blocked = conn.execute("SELECT coalesce(max(blocked_until),0) FROM github_state").fetchone()[0]
            state = conn.execute("SELECT * FROM github_state WHERE project_id=?", (project_id,)).fetchone()
            if state["next_poll_at"] > now or blocked > now:
                return "waiting"
            if state["lease_until"] > now:
                return "busy"
            conn.execute("UPDATE github_state SET lease_owner=?,lease_until=? WHERE project_id=?",
                         (owner, now + self.settings.request_timeout_seconds * 3 + 30, project_id))
        try:
            branch = state["tracked_branch"]
            if not branch:
                branch = self.reader.repository(project["repo_full_name"]).default_branch
            head = self.reader.head(project["repo_full_name"], branch, project["etag"])
            if head.not_modified and project["last_seen_sha"] is None:
                # A manually damaged local cursor cannot safely use a 304.
                head = self.reader.head(project["repo_full_name"], branch, None)
            if not head.not_modified:
                self.queue.observe_head(project_id, head.sha, etag=head.etag, lease_owner=owner)
            with self.db.transaction() as conn:
                if head.not_modified:
                    conn.execute("""UPDATE projects SET etag=? WHERE id=? AND EXISTS
                        (SELECT 1 FROM github_state WHERE project_id=? AND lease_owner=? AND lease_until>?)""",
                                 (head.etag, project_id, project_id, owner, self.clock()))
                conn.execute("""UPDATE github_state SET tracked_branch=?,last_polled_at=?,next_poll_at=?,
                    consecutive_errors=0,last_error=NULL,lease_owner=NULL,lease_until=0
                    WHERE project_id=? AND lease_owner=?""",
                             (branch, self.clock(), self.clock() + max(self.settings.poll_seconds, head.poll_interval), project_id, owner))
            return "unchanged" if head.not_modified else "observed"
        except GitHubError as error:
            self._failed(project_id, owner, error)
            return str(error)
        except Exception:
            self._failed(project_id, owner, GitHubError("invalid_response"))
            return "invalid_response"

    def _failed(self, project_id: str, owner: str, error: GitHubError):
        with self.db.transaction() as conn:
            state = conn.execute("SELECT * FROM github_state WHERE project_id=? AND lease_owner=?", (project_id, owner)).fetchone()
            if state is None:
                return
            failures = state["consecutive_errors"] + 1
            delay = min(self.settings.github_backoff_max_seconds,
                        self.settings.poll_seconds * 2 ** min(failures - 1, self.settings.max_http_retries))
            next_at = max(self.clock() + delay, error.retry_at)
            blocked = next_at if str(error) == "rate_limited" else state["blocked_until"]
            conn.execute("""UPDATE github_state SET last_polled_at=?,next_poll_at=?,consecutive_errors=?,
                last_error=?,blocked_until=?,lease_owner=NULL,lease_until=0 WHERE project_id=? AND lease_owner=?""",
                         (self.clock(), next_at, failures, str(error), blocked, project_id, owner))
            if str(error) in {"access_denied", "not_found"}:
                conn.execute("UPDATE projects SET status='pending_access',etag=NULL WHERE id=? AND status='active'", (project_id,))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'github.poll_deferred','application',?)",
                         (project_id, json.dumps({"reason": str(error), "retry_at": next_at})))

    def tick(self) -> list[dict]:
        # One pass can be invoked by a thread scheduler, CLI, or deterministic test.
        if not self._lock.acquire(blocking=False):
            return []
        try:
            with self.db.transaction(write=False) as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM projects WHERE status='active' AND baseline_confirmed_at IS NOT NULL ORDER BY id")]
            results = []
            for project_id in ids:
                result = self.poll_project(project_id)
                cycle_id = self.queue.flush(project_id)
                results.append({"project_id": project_id, "poll": result, "cycle_id": cycle_id})
            return results
        finally:
            self._lock.release()


class PollingScheduler:
    def __init__(self, trigger: PushTrigger, *, interval_seconds: float = 1):
        if interval_seconds <= 0:
            raise ValueError("Scheduler interval must be positive")
        self.trigger, self.interval_seconds = trigger, interval_seconds
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="supervisorai-github-poll", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.trigger.tick()
                self.last_error = None
            except Exception:
                self.last_error = "polling_worker_error"
            self._stop.wait(self.interval_seconds)

    def close(self, timeout: float = 35):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError("Polling worker is still stopping")
