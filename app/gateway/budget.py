"""SQLite-backed reservations survive crashes and serialize concurrent callers."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from app.config import Settings
from app.db import Database
from app.gateway.types import AgentSpec, BudgetExceeded


class BudgetLedger:
    def __init__(self, db: Database, settings: Settings, clock):
        self.db, self.settings, self.clock = db, settings, clock

    def day(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()

    def _remaining(self, conn, job: dict) -> dict:
        pid, cid = job["project_id"], job["cycle_id"]
        count = conn.execute(
            "SELECT count(*) FROM llm_calls WHERE project_id=? AND cycle_id=? AND attempted=1", (pid, cid),
        ).fetchone()[0]
        # Deliberate aggregate across projects for the supervisor's global batch.
        batch_count = conn.execute(
            "SELECT count(*) FROM llm_calls WHERE batch_id=? AND attempted=1", (job["batch_id"],),
        ).fetchone()[0]
        agent_count = conn.execute(
            "SELECT count(*) FROM llm_calls WHERE project_id=? AND cycle_id=? AND agent=? AND attempted=1",
            (pid, cid, job["agent"]),
        ).fetchone()[0]
        tool_count = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE project_id=? AND cycle_id=? AND agent=?",
            (pid, cid, job["agent"]),
        ).fetchone()[0]
        row = conn.execute("SELECT tokens FROM usage_quota WHERE project_id=? AND day=?", (pid, self.day())).fetchone()
        return {
            "cycle_calls": max(0, self.settings.max_llm_calls_per_cycle - count),
            "batch_calls": max(0, self.settings.max_llm_calls_per_batch - batch_count),
            "agent_calls": max(0, (self.settings.max_coordinator_calls if job["agent"] == "coordinator"
                                   else self.settings.max_llm_calls_per_cycle) - agent_count),
            "tool_calls": max(0, self.settings.max_specialist_tool_calls - tool_count),
            "daily_tokens": max(0, self.settings.daily_token_quota - (row[0] if row else 0)),
        }

    def remaining(self, job: dict) -> dict:
        with self.db.transaction(write=False) as conn:
            return self._remaining(conn, job)

    def reserve(self, job: dict, tokens_in: int, tokens_out: int) -> str:
        cost = tokens_in + tokens_out
        with self.db.transaction() as conn:
            remaining = self._remaining(conn, job)
            for dimension in ("cycle_calls", "batch_calls", "agent_calls"):
                if remaining[dimension] <= 0:
                    raise BudgetExceeded(dimension)
            if remaining["daily_tokens"] < cost:
                raise BudgetExceeded("daily_tokens")
            call_id, day = uuid4().hex, self.day()
            conn.execute(
                """INSERT INTO usage_quota(project_id,day,tokens) VALUES (?,?,?)
                   ON CONFLICT(project_id,day) DO UPDATE SET tokens=tokens+excluded.tokens""",
                (job["project_id"], day, cost),
            )
            conn.execute(
                """INSERT INTO llm_calls(id,project_id,cycle_id,agent,model,tokens_in,tokens_out,
                   outcome,batch_id,quota_day,charged_tokens,attempted)
                   VALUES (?,?,?,?,?,?,?,'reserved',?,?,?,1)""",
                (call_id, job["project_id"], job["cycle_id"], job["agent"], job["model"], tokens_in,
                 tokens_out, job["batch_id"], day, cost),
            )
        return call_id

    def finish(self, job: dict, call_id: str, *, outcome: str, latency_ms: float,
               tokens_in: int | None = None, tokens_out: int | None = None, http_status: int | None = None) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM llm_calls WHERE project_id=? AND id=?", (job["project_id"], call_id)).fetchone()
            if row is None or row["outcome"] != "reserved":
                return
            # No refund on ambiguous transport failure or unknown provider usage.
            known = tokens_in is not None and tokens_out is not None
            actual = tokens_in + tokens_out if known else row["charged_tokens"]
            conn.execute("UPDATE usage_quota SET tokens=tokens+? WHERE project_id=? AND day=?",
                         (actual - row["charged_tokens"], job["project_id"], row["quota_day"]))
            conn.execute(
                """UPDATE llm_calls SET outcome=?,latency_ms=?,tokens_in=?,tokens_out=?,charged_tokens=?,http_status=?
                   WHERE project_id=? AND id=?""",
                (outcome, max(0, latency_ms), tokens_in if known else row["tokens_in"],
                 tokens_out if known else row["tokens_out"], actual, http_status, job["project_id"], call_id),
            )

    def log_local(self, job: dict, outcome: str, *, cache_hit: bool = False) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO llm_calls(id,project_id,cycle_id,agent,model,outcome,batch_id,cache_hit)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (uuid4().hex, job["project_id"], job["cycle_id"], job["agent"], job["model"], outcome,
                 job["batch_id"], int(cache_hit)),
            )

    def run_tool(self, project_id: str, cycle_id: str, spec: AgentSpec, tool: str, callback):
        """Called only by trusted Python orchestration; LLM responses cannot invoke it."""
        if tool not in spec.allowed_tools:
            raise ValueError("Tool not allowed for this agent")
        with self.db.transaction() as conn:
            count = conn.execute("SELECT count(*) FROM tool_calls WHERE project_id=? AND cycle_id=? AND agent=?",
                                 (project_id, cycle_id, spec.name)).fetchone()[0]
            if count >= self.settings.max_specialist_tool_calls:
                reason = "tool_calls"
                conn.execute("UPDATE cycles SET status='pending_evidence_review' WHERE project_id=? AND id=?",
                             (project_id, cycle_id))
                conn.execute("""INSERT INTO checkpoints(project_id,cycle_id,stage,payload) VALUES (?,?,?,?)
                    ON CONFLICT(project_id,cycle_id,stage) DO UPDATE SET payload=excluded.payload""",
                             (project_id, cycle_id, "gateway_tools:" + spec.name, json.dumps({"reason": reason})))
                call_id = None
            else:
                call_id = uuid4().hex
                conn.execute("INSERT INTO tool_calls(id,project_id,cycle_id,agent,tool,outcome) VALUES (?,?,?,?,?,'started')",
                             (call_id, project_id, cycle_id, spec.name, tool))
        if call_id is None:
            raise BudgetExceeded("tool_calls")
        outcome = "failed"
        try:
            value = callback()
            outcome = "success"
            return value
        finally:
            with self.db.transaction() as conn:
                conn.execute("UPDATE tool_calls SET outcome=? WHERE project_id=? AND id=?", (outcome, project_id, call_id))
