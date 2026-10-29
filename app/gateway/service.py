"""One durable round-robin queue per database; provider calls only happen here."""

from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import random
import threading
import time
from uuid import uuid4

from pydantic import ValidationError

from app.config import Settings
from app.db import Database, ProjectStore
from app.gateway.budget import BudgetLedger
from app.gateway.clients import FakeLLMClient, GroqClient
from app.gateway.limiter import ModelLimiter, retry_after_seconds
from app.gateway.prompts import canonical, digest, estimate_tokens, messages, spec_hash
from app.gateway.types import AgentSpec, BudgetExceeded, CompletionRequest, GatewayError, JobResult, ProviderError


class _WorkerLock:
    """OS releases the lock on process death; a second worker cannot recover live jobs."""

    def __init__(self, path: Path):
        self.file = open(path, "a+b")
        try:
            if path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise GatewayError("gateway_already_running") from None

    def close(self):
        self.file.close()


class Gateway:
    def __init__(self, db: Database, settings: Settings, specs: list[AgentSpec], *, client=None,
                 clock=time.time, jitter=random.random, start: bool = True):
        self.db, self.settings, self.clock, self.jitter = db, settings, clock, jitter
        self.specs = {spec.name: spec for spec in specs}
        if len(self.specs) != len(specs):
            raise ValueError("Duplicate agent specification")
        self._lease = _WorkerLock(db.path.with_suffix(".gateway.lock"))
        self._closed = False
        self._condition = threading.Condition()
        self._execution = threading.Lock()
        self._thread = None
        self._worker_error = None
        self._last_project = ""
        self.ledger = BudgetLedger(db, settings, clock)
        self.limiter = ModelLimiter(settings)
        try:
            self.client = client if client is not None else (FakeLLMClient() if settings.llm_mode == "fake" else GroqClient(settings))
            with db.transaction() as conn:
                conn.execute("UPDATE gateway_jobs SET status='queued' WHERE status='running'")
                # Keep all pre-flight reservations charged after an interrupted call.
                conn.execute("UPDATE llm_calls SET outcome='interrupted' WHERE outcome='reserved'")
                observations = conn.execute("SELECT * FROM gateway_model_limits ORDER BY observed_at").fetchall()
            for row in observations:
                self.limiter.observe(row["model"], json.loads(row["headers_json"]), row["observed_at"])
                self.limiter.block(row["model"], row["blocked_until"], row["observed_at"])
            if start:
                self._thread = threading.Thread(target=self._worker, name="supervisorai-llm", daemon=True)
                self._thread.start()
        except BaseException:
            self._lease.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=self.settings.request_timeout_seconds + 5)
            if self._thread.is_alive():
                raise GatewayError("worker_still_stopping")
        with self._execution:
            self.client.close()
            self._lease.close()

    def _model(self, spec: AgentSpec) -> str:
        if self.settings.llm_mode == "fake":
            return "fake/" + spec.tier  # Fake namespace prevents pollution of live cache.
        return self.settings.groq_model_fast if spec.tier == "fast" else self.settings.groq_model_smart

    def submit(self, project_id: str, cycle_id: str, batch_id: str, agent: str, *, data: dict | None = None,
               evidence_ids: tuple[str, ...] | list[str] = (), connection=None) -> str:
        if self._closed:
            raise GatewayError("gateway_closed")
        if not batch_id or len(batch_id) > 200:
            raise ValueError("A bounded, explicit batch ID is required")
        spec = self.specs[agent]
        store = ProjectStore(self.db, project_id)
        evidence = []
        for evidence_id in evidence_ids:
            item = store.evidence(evidence_id)
            if item is None:
                raise ValueError("Evidence does not belong to this project")
            evidence.append({key: item[key] for key in ("id", "type", "ref", "content_hash", "content", "meta_json")})
        payload = {"data": data or {}, "evidence": evidence}
        encoded = canonical(payload)  # Snapshot caller data before queuing; no mutable references.
        model, fingerprint = self._model(spec), spec_hash(spec)
        key = digest({"project_id": project_id, "model": model, "mode": self.settings.llm_mode, "spec": fingerprint, "payload": payload,
                      "max_input": self.settings.max_input_tokens, "max_output": self.settings.max_output_tokens})
        request_key = digest({"cycle": cycle_id, "cache_key": key})
        job_id = uuid4().hex
        # Trusted application flows can enqueue atomically with their own state.
        # The caller owns commit/rollback when passing a connection to this DB.
        with self.db.transaction() if connection is None else nullcontext(connection) as conn:
            if conn.execute("SELECT id FROM cycles WHERE project_id=? AND id=?", (project_id, cycle_id)).fetchone() is None:
                raise ValueError("Cycle does not belong to this project")
            conn.execute("""INSERT INTO gateway_jobs(id,project_id,cycle_id,batch_id,agent,model,spec_hash,cache_key,request_key,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,request_key) DO NOTHING""",
                         (job_id, project_id, cycle_id, batch_id, agent, model, fingerprint, key, request_key, encoded))
            job_id = conn.execute("SELECT id FROM gateway_jobs WHERE project_id=? AND request_key=?", (project_id, request_key)).fetchone()[0]
        with self._condition:
            self._condition.notify_all()
        return job_id

    def result(self, project_id: str, job_id: str) -> JobResult:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM gateway_jobs WHERE project_id=? AND id=?", (project_id, job_id)).fetchone()
        if row is None:
            raise LookupError("Job not found for project")
        value = None
        if row["status"] == "completed":
            spec = self.specs.get(row["agent"])
            if spec is None or spec_hash(spec) != row["spec_hash"]:
                raise GatewayError("agent_spec_changed")
            value = spec.output_schema.model_validate_json(row["response_json"], strict=True)
        return JobResult(job_id=job_id, status=row["status"], value=value, reason=row["reason"])

    def wait(self, project_id: str, job_id: str, timeout: float = 30) -> JobResult:
        """For scripts/tests only; UI should submit and poll result/status."""
        deadline = time.monotonic() + timeout
        while True:
            result = self.result(project_id, job_id)
            if result.status not in {"queued", "running"}:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Gateway wait timed out; job remains persisted")
            with self._condition:
                self._condition.wait(timeout=min(.25, remaining))

    def resume(self, project_id: str, job_id: str, *, batch_id: str | None = None, connection=None) -> None:
        """Explicit application retry; counters/repair allowance are never reset."""
        if batch_id is not None and (not batch_id or len(batch_id) > 200):
            raise ValueError("Invalid batch ID")
        with (nullcontext(connection) if connection is not None else self.db.transaction()) as conn:
            row = conn.execute("SELECT status,reason FROM gateway_jobs WHERE project_id=? AND id=?", (project_id, job_id)).fetchone()
            if row is None:
                raise LookupError("Job not found for project")
            if row[0] != "pending":
                raise ValueError("Only pending jobs can resume")
            if row["reason"] in {"invalid_output", "retries_exhausted"}:
                raise ValueError("Retry allowance exhausted; new evidence or a revised request is required")
            conn.execute("""UPDATE gateway_jobs SET status='queued',reason=NULL,available_at=0,batch_id=coalesce(?,batch_id)
                WHERE project_id=? AND id=?""", (batch_id, project_id, job_id))
        with self._condition:
            self._condition.notify_all()

    def repair_output(self, project_id: str, job_id: str) -> bool:
        """Application validation rejected a completed result; use the SAME repair cap.

        No raw provider result is echoed in the repair prompt. Reservations and
        attempt counts remain intact, including when the invalid result was cached.
        """
        with self.db.transaction() as conn:
            row = conn.execute('SELECT status,repair_count,cache_key FROM gateway_jobs WHERE project_id=? AND id=?', (project_id, job_id)).fetchone()
            if row is None:
                raise LookupError('Job not found for project')
            if row['status'] != 'completed':
                return False
            conn.execute('DELETE FROM llm_cache WHERE project_id=? AND cache_key=?', (project_id, row['cache_key']))
            if row['repair_count'] >= self.settings.json_repair_retries:
                return False
            conn.execute("UPDATE gateway_jobs SET status='queued',reason='json_repair',response_json=NULL,repair_count=repair_count+1,available_at=0 WHERE project_id=? AND id=?", (project_id, job_id))
        with self._condition:
            self._condition.notify_all()
        return True

    def run_once(self) -> bool:
        """Run one eligible attempt, then give the next project a turn."""
        with self._execution:
            if self._closed:
                return False
            now = self.clock()
            with self.db.transaction() as conn:
                projects = [row[0] for row in conn.execute(
                    "SELECT DISTINCT project_id FROM gateway_jobs WHERE status='queued' AND available_at<=? ORDER BY project_id", (now,))]
                if not projects:
                    return False
                pid = next((pid for pid in projects if pid > self._last_project), projects[0])
                row = conn.execute("SELECT * FROM gateway_jobs WHERE project_id=? AND status='queued' AND available_at<=? ORDER BY seq LIMIT 1",
                                   (pid, now)).fetchone()
                job = dict(row)
                conn.execute("UPDATE gateway_jobs SET status='running' WHERE project_id=? AND id=?", (pid, job["id"]))
            self._last_project = pid
            try:
                self._attempt(job)
            except Exception:
                # Do not let raw provider/validation/transport exceptions reach UI or logs.
                self._pending(job, "internal_error")
            with self._condition:
                self._condition.notify_all()
            return True

    def _attempt(self, job: dict):
        spec = self.specs.get(job["agent"])
        if spec is None or spec_hash(spec) != job["spec_hash"] or self._model(spec) != job["model"]:
            self._pending(job, "agent_spec_changed")
            return
        store = ProjectStore(self.db, job["project_id"])
        cached = store.cache_get(job["cache_key"])
        if cached is not None:
            try:
                value = spec.output_schema.model_validate_json(canonical(cached), strict=True)
            except ValidationError:
                pass  # Never return corrupt or stale cached output.
            else:
                self.ledger.log_local(job, "cache_hit", cache_hit=True)
                self._complete(job, value)
                return
        remaining = self.ledger.remaining(job)
        chat = messages(spec, json.loads(job["payload_json"]), remaining, self.settings.max_output_tokens,
                        repair=job["repair_count"] > 0)
        estimated = estimate_tokens(chat)
        if estimated > self.settings.max_input_tokens:
            self.ledger.log_local(job, "input_too_large")
            self._pending(job, "input_too_large")
            return
        cost = estimated + self.settings.max_output_tokens
        delay = self.limiter.delay(job["model"], cost, self.clock())
        if math.isinf(delay):
            self._pending(job, "model_token_capacity")
            return
        if delay > .001:
            self._requeue(job, delay, "model_rate_limit")
            return
        try:
            call_id = self.ledger.reserve(job, estimated, self.settings.max_output_tokens)
        except BudgetExceeded as error:
            self.ledger.log_local(job, "budget_exhausted:" + str(error))
            self._pending(job, str(error))
            return
        self.limiter.consume(job["model"], cost, self.clock())
        started = time.monotonic()
        request = CompletionRequest(job["project_id"], job["cycle_id"], job["agent"], job["model"], chat,
                                    self.settings.max_output_tokens, estimated)
        try:
            response = self.client.complete(request)
        except ProviderError as error:
            self.ledger.finish(job, call_id, outcome=str(error), latency_ms=(time.monotonic() - started) * 1000, http_status=error.status)
            self._observe(job, error.headers)
            if str(error) == "provider_json_error":
                if job["repair_count"] < self.settings.json_repair_retries:
                    self._requeue(job, 0, "json_repair", repair=True)
                else:
                    self._pending(job, "invalid_output")
                return
            if error.retryable and job["retry_count"] < self.settings.max_http_retries:
                delay = min(self.settings.llm_backoff_max_seconds,
                            self.settings.llm_backoff_base_seconds * 2 ** job["retry_count"])
                delay = min(self.settings.llm_backoff_max_seconds, delay + min(1.0, max(0.0, self.jitter())) * delay * .25)
                lower_headers = {key.lower(): value for key, value in error.headers.items()}
                delay = max(delay, retry_after_seconds(lower_headers.get("retry-after"), self.clock()))
                if error.status == 429:
                    self._observe(job, error.headers, blocked_until=self.clock() + delay)
                self._requeue(job, delay, str(error), retry=True)
            else:
                self._pending(job, "retries_exhausted" if error.retryable else str(error))
            return
        except Exception:
            self.ledger.finish(job, call_id, outcome="client_error", latency_ms=(time.monotonic() - started) * 1000)
            self._pending(job, "client_error")
            return
        self._observe(job, response.headers)
        try:
            if response.finish_reason != "stop" or (response.tokens_out is not None and response.tokens_out > self.settings.max_output_tokens):
                raise ValueError("output_limit")
            value = spec.output_schema.model_validate_json(response.content, strict=True)
        except (ValidationError, ValueError):
            self.ledger.finish(job, call_id, outcome="invalid_output", latency_ms=(time.monotonic() - started) * 1000,
                               tokens_in=response.tokens_in, tokens_out=response.tokens_out, http_status=200)
            if job["repair_count"] < self.settings.json_repair_retries:
                self._requeue(job, 0, "json_repair", repair=True)
            else:
                self._pending(job, "invalid_output")
            return
        self.ledger.finish(job, call_id, outcome="success", latency_ms=(time.monotonic() - started) * 1000,
                           tokens_in=response.tokens_in, tokens_out=response.tokens_out, http_status=200)
        self._complete(job, value)

    def _complete(self, job: dict, value):
        encoded = value.model_dump_json()
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO llm_cache(project_id,cache_key,model,response_json) VALUES (?,?,?,?)
                ON CONFLICT(project_id,cache_key) DO UPDATE SET response_json=excluded.response_json""",
                         (job["project_id"], job["cache_key"], job["model"], encoded))
            conn.execute("UPDATE gateway_jobs SET status='completed',response_json=?,reason=NULL WHERE project_id=? AND id=?",
                         (encoded, job["project_id"], job["id"]))
            conn.execute("DELETE FROM checkpoints WHERE project_id=? AND cycle_id=? AND stage=?",
                         (job["project_id"], job["cycle_id"], "gateway:" + job["id"]))
        # The orchestrator owns the cycle stage and decides when the cycle can run again.

    def _observe(self, job: dict, headers: dict, *, blocked_until: float = 0):
        safe_headers = {key.lower(): value for key, value in headers.items()
                        if key.lower().startswith("x-ratelimit-") or key.lower() == "retry-after"}
        now = self.clock()
        self.limiter.observe(job["model"], safe_headers, now)
        self.limiter.block(job["model"], blocked_until, now)
        if not safe_headers and not blocked_until:
            return
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO gateway_model_limits(project_id,model,observed_at,headers_json,blocked_until)
                VALUES (?,?,?,?,?) ON CONFLICT(project_id,model) DO UPDATE SET
                observed_at=excluded.observed_at,headers_json=excluded.headers_json,
                blocked_until=max(blocked_until,excluded.blocked_until)""",
                         (job["project_id"], job["model"], now, canonical(safe_headers), blocked_until))

    def _pending(self, job: dict, reason: str):
        with self.db.transaction() as conn:
            conn.execute("UPDATE gateway_jobs SET status='pending',reason=? WHERE project_id=? AND id=?",
                         (reason, job["project_id"], job["id"]))
            conn.execute("UPDATE cycles SET status='pending_evidence_review' WHERE project_id=? AND id=?",
                         (job["project_id"], job["cycle_id"]))
            conn.execute("""INSERT INTO checkpoints(project_id,cycle_id,stage,payload) VALUES (?,?,?,?)
                ON CONFLICT(project_id,cycle_id,stage) DO UPDATE SET payload=excluded.payload""",
                         (job["project_id"], job["cycle_id"], "gateway:" + job["id"], canonical({"job_id": job["id"], "reason": reason})))

    def _requeue(self, job: dict, delay: float, reason: str, *, retry=False, repair=False):
        with self.db.transaction() as conn:
            conn.execute("""UPDATE gateway_jobs SET status='queued',available_at=?,reason=?,
                retry_count=retry_count+?,repair_count=repair_count+? WHERE project_id=? AND id=?""",
                         (self.clock() + delay, reason, int(retry), int(repair), job["project_id"], job["id"]))

    def _worker(self):
        while not self._closed:
            try:
                if self.run_once():
                    self._worker_error = None
                    continue
            except Exception:
                # DB errors must not spin the worker or leak content. Durable jobs
                # remain recoverable; callers can inspect the queue and restart.
                self._worker_error = "worker_storage_error"
            with self._condition:
                if not self._closed:
                    self._condition.wait(timeout=.25)

    def status(self, project_id: str) -> dict:
        ProjectStore(self.db, project_id)
        day = self.ledger.day()
        with self.db.transaction(write=False) as conn:
            counts = {row[0]: row[1] for row in conn.execute(
                "SELECT status,count(*) FROM gateway_jobs WHERE project_id=? GROUP BY status", (project_id,))}
            total = conn.execute("SELECT count(*) FROM gateway_jobs WHERE status IN ('queued','running')").fetchone()[0]
            usage = conn.execute("SELECT tokens FROM usage_quota WHERE project_id=? AND day=?", (project_id, day)).fetchone()
            next_at = conn.execute("SELECT min(available_at) FROM gateway_jobs WHERE project_id=? AND status='queued'", (project_id,)).fetchone()[0]
        tomorrow = datetime.fromtimestamp(self.clock(), timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return {"project_id": project_id, "queue_depth": total, "project_jobs": counts,
                "daily_tokens_used": usage[0] if usage else 0, "daily_token_limit": self.settings.daily_token_quota,
                "worker_error": self._worker_error, "model_limits": self.limiter.snapshot(self.clock()),
                "quota_resets_in_seconds": max(0, tomorrow.timestamp() - self.clock()),
                "next_attempt_in_seconds": max(0, (next_at or 0) - self.clock())}


_shared_lock = threading.Lock()
_shared: dict[str, Gateway] = {}


def get_shared_gateway(db: Database, settings: Settings, specs: list[AgentSpec]) -> Gateway:
    """UI reruns reuse one worker. Close explicitly on application shutdown."""
    with _shared_lock:
        key = str(db.path)
        gateway = _shared.get(key)
        if gateway is None or gateway._closed:
            gateway = Gateway(db, settings, specs)
            _shared[key] = gateway
        elif gateway.settings != settings or {name: spec_hash(spec) for name, spec in gateway.specs.items()} != {spec.name: spec_hash(spec) for spec in specs}:
            raise GatewayError("gateway_configuration_changed_restart_required")
        return gateway
