"""Persisted onboarding flow: upload -> bounded extraction -> edit -> confirm."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from app.agents.baseline import BaselineDraft, baseline_spec
from app.db import Database, ProjectStore
from app.gateway import Gateway
from app.gateway.prompts import canonical, estimate_tokens, messages, spec_hash
from app.integrations.pdf.retrieval import Retriever
from app.integrations.pdf.store import PDFStore


class DraftConflict(ValueError):
    pass


class BaselineService:
    def __init__(self, db: Database, pdfs: PDFStore, gateway: Gateway | None = None):
        self.db, self.pdfs, self.gateway = db, pdfs, gateway
        self.retriever = Retriever(pdfs)
        if gateway is not None and ("baseline" not in gateway.specs or spec_hash(gateway.specs["baseline"]) != spec_hash(baseline_spec())):
            raise ValueError("Gateway must register baseline_spec()")

    def upload(self, project_id: str, content: bytes, *, filename="proposal.pdf") -> dict:
        if ProjectStore(self.db, project_id).project()["baseline_confirmed_at"]:
            raise ValueError("Baseline is already confirmed; proposal replacement is not allowed")
        document = self.pdfs.ingest(project_id, content, kind="proposal", filename=filename)
        with self.db.transaction() as conn:
            if conn.execute("SELECT baseline_confirmed_at FROM projects WHERE id=?", (project_id,)).fetchone()[0]:
                # Ingestion raced with another confirmation. Do not block later reviews.
                conn.execute("UPDATE cycles SET status='skipped' WHERE project_id=? AND id=? AND status='queued'",
                             (project_id, document["cycle_id"]))
                conflict = True
            else:
                conflict = False
                conn.execute("""INSERT INTO baseline_drafts(id,project_id,document_id,cycle_id) VALUES (?,?,?,?)
                    ON CONFLICT(project_id,document_id) DO NOTHING""", (uuid4().hex, project_id, document["id"], document["cycle_id"]))
                draft_id = conn.execute("SELECT id FROM baseline_drafts WHERE project_id=? AND document_id=?", (project_id, document["id"])).fetchone()[0]
        if conflict:
            raise DraftConflict("Baseline was confirmed during upload")
        return self.get(project_id, draft_id)

    def get(self, project_id: str, draft_id: str) -> dict:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM baseline_drafts WHERE project_id=? AND id=?", (project_id, draft_id)).fetchone()
        if row is None:
            raise LookupError("Baseline draft not found for project")
        result = dict(row)
        result["draft"] = json.loads(result.pop("draft_json"))
        result["context"] = json.loads(result.pop("context_json"))
        return result

    def drafts(self, project_id: str) -> list[dict]:
        ProjectStore(self.db, project_id)
        with self.db.transaction() as conn:
            # Recover a stop after PDF ingestion but before upload() created its draft.
            if not conn.execute("SELECT baseline_confirmed_at FROM projects WHERE id=?", (project_id,)).fetchone()[0]:
                for document in conn.execute("""SELECT p.id,p.cycle_id FROM pdf_documents p
                    WHERE p.project_id=? AND p.kind='proposal' AND NOT EXISTS
                    (SELECT 1 FROM baseline_drafts b WHERE b.project_id=p.project_id AND b.document_id=p.id)""", (project_id,)).fetchall():
                    conn.execute("INSERT INTO baseline_drafts(id,project_id,document_id,cycle_id) VALUES (?,?,?,?)",
                                 (uuid4().hex, project_id, document["id"], document["cycle_id"]))
            ids = [row[0] for row in conn.execute("SELECT id FROM baseline_drafts WHERE project_id=? ORDER BY created_at,id", (project_id,))]
        return [self.get(project_id, draft_id) for draft_id in ids]

    def _context(self, draft: dict, batch_id: str) -> tuple[dict, dict]:
        spec = self.gateway.specs["baseline"]
        passages = self.retriever.passages(draft["project_id"], draft["document_id"])
        ranked = self.retriever.retrieve(draft["project_id"], draft["document_id"],
                                        "objective objectives milestone milestones criteria deadline schedule timeline deliverable evaluation", top_k=max(1, len(passages)))
        selected_keys = {(p.page, p.start) for p in ranked}
        ranked += [p for p in passages if (p.page, p.start) not in selected_keys]
        remaining = self.gateway.ledger.remaining({"project_id": draft["project_id"], "cycle_id": draft["cycle_id"], "agent": "baseline", "batch_id": batch_id})
        context = {"pages": [], "partial": True}
        segments = []
        def fits(candidate):
            # Reserve enough context space for the configured repair prompt as well.
            chat = messages(spec, {"data": candidate, "evidence": []}, remaining,
                            self.gateway.settings.max_output_tokens, repair=bool(self.gateway.settings.json_repair_retries))
            return estimate_tokens(chat) <= self.gateway.settings.max_input_tokens - 32
        for passage in ranked:
            low, high, best = 0, len(passage.text), 0
            while low <= high:
                middle = (low + high) // 2
                candidate = {"pages": context["pages"] + [{"page": passage.page, "text": passage.text[:middle]}], "partial": True}
                if fits(candidate):
                    best, low = middle, middle + 1
                else:
                    high = middle - 1
            if best < min(20, len(passage.text)):
                continue
            context["pages"].append({"page": passage.page, "text": passage.text[:best]})
            segments.append({"page": passage.page, "start": passage.start, "end": passage.start + best})
        document = self.pdfs.get(draft["project_id"], draft["document_id"])
        included = sum(len(page["text"]) for page in context["pages"])
        blank_pages = [page["page_number"] for page in self.pdfs.pages(draft["project_id"], draft["document_id"]) if not page["content"].strip()]
        context["partial"] = included < document["text_chars"] or bool(blank_pages)
        coverage = {"included_chars": included, "total_chars": document["text_chars"], "segments": segments,
                    "blank_pages": blank_pages, "input": context}
        return context, coverage

    def request_extraction(self, project_id: str, draft_id: str, batch_id: str) -> dict:
        if self.gateway is None:
            raise ValueError("Extraction requires a gateway")
        draft = self.get(project_id, draft_id)
        if draft["status"] == "pending" and draft["job_id"]:
            # Explicit retry of the SAME job: preserve calls, quotas and repair
            # counters. Serialize with edits so a retry cannot overwrite them.
            with self.db.transaction() as conn:
                current = conn.execute("SELECT status,revision FROM baseline_drafts WHERE project_id=? AND id=?", (project_id, draft_id)).fetchone()
                if current["status"] != "pending" or current["revision"] != draft["revision"]:
                    raise DraftConflict("Draft changed; reload before retrying")
                self.gateway.resume(project_id, draft["job_id"], connection=conn)
                conn.execute("UPDATE baseline_drafts SET status='extracting',reason=NULL WHERE project_id=? AND id=?", (project_id, draft_id))
            return self.get(project_id, draft_id)
        if draft["status"] != "uploaded":
            return draft  # Never overwrite edits or create a second extraction job.
        document = self.pdfs.get(project_id, draft["document_id"])
        context, coverage = self._context(draft, batch_id)
        reason = "no_extractable_text" if document["status"] == "no_text" else "input_too_large" if not context["pages"] else None
        # Queue insertion and draft transition commit together, even with a live
        # background worker or concurrent submission of this draft.
        with self.db.transaction() as conn:
            current = conn.execute("SELECT status,revision FROM baseline_drafts WHERE project_id=? AND id=?", (project_id, draft_id)).fetchone()
            if current["status"] != "uploaded" or current["revision"] != draft["revision"]:
                return self.get(project_id, draft_id)
            conn.execute("UPDATE baseline_drafts SET context_json=?,limited_coverage=?,reason=? WHERE project_id=? AND id=?",
                         (canonical(coverage), int(context["partial"]), reason, project_id, draft_id))
            if reason:
                conn.execute("UPDATE baseline_drafts SET status='pending' WHERE project_id=? AND id=?", (project_id, draft_id))
                conn.execute("UPDATE cycles SET status='pending_evidence_review' WHERE project_id=? AND id=?", (project_id, draft["cycle_id"]))
                conn.execute("""INSERT INTO checkpoints(project_id,cycle_id,stage,payload) VALUES (?,?,'baseline_extraction',?)
                    ON CONFLICT(project_id,cycle_id,stage) DO UPDATE SET payload=excluded.payload""",
                             (project_id, draft["cycle_id"], canonical({"draft_id": draft_id, "reason": reason})))
            else:
                job_id = self.gateway.submit(project_id, draft["cycle_id"], batch_id, "baseline", data=context, connection=conn)
                conn.execute("UPDATE baseline_drafts SET job_id=?,status='extracting' WHERE project_id=? AND id=?", (job_id, project_id, draft_id))
        return self.get(project_id, draft_id)

    def collect(self, project_id: str, draft_id: str) -> dict:
        draft = self.get(project_id, draft_id)
        if not draft["job_id"] or draft["status"] not in {"extracting", "pending"}:
            return draft
        if self.gateway is None:
            raise ValueError("Collect requires a gateway with the registered schema")
        result = self.gateway.result(project_id, draft["job_id"])
        if result.status in {"queued", "running"}:
            return draft
        value = draft["draft"]
        status, reason = "pending", result.reason
        if result.status == "completed":
            allowed_pages = {p["page"] for p in draft["context"]["input"]["pages"]}
            value = result.value.model_dump(mode="json")
            bad_refs = any(page not in allowed_pages for milestone in result.value.milestones for page in milestone.source_pages)
            status, reason = ("pending", "unsupported_source_pages") if bad_refs else ("editable", None)
        if status == draft["status"] and reason == draft["reason"] and value == draft["draft"]:
            return draft
        with self.db.transaction() as conn:
            updated = conn.execute("""UPDATE baseline_drafts SET draft_json=?,status=?,reason=?,revision=revision+1
                WHERE project_id=? AND id=? AND revision=? AND status IN ('extracting','pending')""",
                         (canonical(value), status, reason, project_id, draft_id, draft["revision"])).rowcount
            if updated and status == "pending":
                conn.execute("UPDATE cycles SET status='pending_evidence_review' WHERE project_id=? AND id=?", (project_id, draft["cycle_id"]))
                conn.execute("""INSERT INTO checkpoints(project_id,cycle_id,stage,payload) VALUES (?,?,'baseline_extraction',?)
                    ON CONFLICT(project_id,cycle_id,stage) DO UPDATE SET payload=excluded.payload""",
                             (project_id, draft["cycle_id"], canonical({"draft_id": draft_id, "reason": reason})))
        return self.get(project_id, draft_id)

    def edit(self, project_id: str, draft_id: str, value: dict | BaselineDraft, *, expected_revision: int) -> dict:
        value = BaselineDraft.model_validate(value)
        draft = self.get(project_id, draft_id)
        page_count = self.pdfs.get(project_id, draft["document_id"])["page_count"]
        if any(page > page_count for item in value.milestones for page in item.source_pages):
            raise ValueError("Source page is outside this document")
        with self.db.transaction() as conn:
            current = conn.execute("SELECT * FROM baseline_drafts WHERE project_id=? AND id=?", (project_id, draft_id)).fetchone()
            if current["status"] in {"confirmed", "superseded"} or current["revision"] != expected_revision:
                raise DraftConflict("Draft changed; reload before editing")
            if current["job_id"]:
                job = conn.execute("SELECT status FROM gateway_jobs WHERE project_id=? AND id=?", (project_id, current["job_id"])).fetchone()
                if job and job[0] == "running":
                    raise DraftConflict("Extraction is running; collect its result before editing")
                conn.execute("UPDATE gateway_jobs SET status='failed',reason='supervisor_edited' WHERE project_id=? AND id=? AND status IN ('queued','pending')",
                             (project_id, current["job_id"]))
            conn.execute("UPDATE baseline_drafts SET draft_json=?,status='editable',reason='supervisor_edited',revision=revision+1 WHERE project_id=? AND id=?",
                         (value.model_dump_json(), project_id, draft_id))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'baseline.edited','supervisor',?)", (project_id, canonical({"draft_id": draft_id})))
        return self.get(project_id, draft_id)

    def confirm(self, project_id: str, draft_id: str, *, expected_revision: int,
                acknowledge_limited_coverage: bool = False) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM baseline_drafts WHERE project_id=? AND id=?", (project_id, draft_id)).fetchone()
            if row is None:
                raise LookupError("Baseline draft not found for project")
            if row["status"] == "confirmed":
                return self.baseline(project_id)
            if row["revision"] != expected_revision or row["status"] != "editable":
                raise DraftConflict("An editable, current draft is required")
            if row["limited_coverage"] and not acknowledge_limited_coverage:
                raise ValueError("Review omitted/blank pages and acknowledge limited extraction coverage")
            project = conn.execute("SELECT baseline_confirmed_at FROM projects WHERE id=?", (project_id,)).fetchone()
            if project[0] or conn.execute("SELECT 1 FROM milestones WHERE project_id=? LIMIT 1", (project_id,)).fetchone():
                raise DraftConflict("A baseline or milestones already exist")
            if conn.execute("SELECT 1 FROM gateway_jobs WHERE project_id=? AND agent='baseline' AND status='running' LIMIT 1", (project_id,)).fetchone():
                raise DraftConflict("Wait for running proposal extractions before confirming")
            value = BaselineDraft.model_validate_json(row["draft_json"])
            if not value.objectives or not value.milestones or any(item.deadline is None for item in value.milestones):
                raise ValueError("Confirmation requires objectives, milestones, and every deadline")
            if len({item.title.casefold() for item in value.milestones}) != len(value.milestones):
                raise ValueError("Milestone titles must be distinct")
            for ordinal, objective in enumerate(value.objectives):
                conn.execute("INSERT INTO project_objectives(id,project_id,baseline_id,ordinal,text) VALUES (?,?,?,?,?)",
                             (uuid4().hex, project_id, draft_id, ordinal, objective))
            for item in value.milestones:
                milestone_id = uuid4().hex
                conn.execute("INSERT INTO milestones(id,project_id,title,completion_criteria,objectives_json) VALUES (?,?,?,?,?)",
                             (milestone_id, project_id, item.title, item.completion_criteria, canonical(value.objectives)))
                conn.execute("""INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,source,decided_by)
                    VALUES (?,?,?,?,'approved','baseline','supervisor')""", (uuid4().hex, project_id, milestone_id, item.deadline))
            conn.execute("UPDATE baseline_drafts SET status='confirmed',confirmed_at=?,revision=revision+1 WHERE project_id=? AND id=?", (now, project_id, draft_id))
            conn.execute("UPDATE projects SET baseline_confirmed_at=? WHERE id=?", (now, project_id))
            conn.execute("UPDATE cycles SET status='completed',stage='baseline_confirmed',finished_at=? WHERE project_id=? AND id=?", (now, project_id, row["cycle_id"]))
            conn.execute("DELETE FROM checkpoints WHERE project_id=? AND cycle_id=?", (project_id, row["cycle_id"]))
            # Abandoned onboarding drafts must not keep the later push queue blocked.
            other_cycles = [r[0] for r in conn.execute("SELECT id FROM cycles WHERE project_id=? AND trigger='proposal_upload' AND id<>?", (project_id, row["cycle_id"]))]
            conn.execute("UPDATE baseline_drafts SET status='superseded',reason='another_baseline_confirmed' WHERE project_id=? AND id<>?", (project_id, draft_id))
            for cycle_id in other_cycles:
                conn.execute("UPDATE cycles SET status='skipped',finished_at=? WHERE project_id=? AND id=?", (now, project_id, cycle_id))
                conn.execute("UPDATE gateway_jobs SET status='failed',reason='another_baseline_confirmed' WHERE project_id=? AND cycle_id=? AND status IN ('queued','pending')", (project_id, cycle_id))
            payload = canonical({"draft_id": draft_id, "limited_coverage_acknowledged": acknowledge_limited_coverage,
                                 "baseline": value.model_dump(mode="json")})
            conn.execute("INSERT INTO supervisor_decisions(id,project_id,kind,target_id,decision,actor,payload_json) VALUES (?,?,'baseline',?,'confirm','supervisor',?)",
                         (uuid4().hex, project_id, draft_id, payload))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'baseline.confirmed','supervisor',?)",
                         (project_id, canonical({"draft_id": draft_id, "milestones": len(value.milestones)})))
        return self.baseline(project_id)

    def baseline(self, project_id: str) -> dict:
        project = ProjectStore(self.db, project_id).project()
        with self.db.transaction(write=False) as conn:
            objectives = [row[0] for row in conn.execute("SELECT text FROM project_objectives WHERE project_id=? ORDER BY ordinal", (project_id,))]
            milestones = [dict(row) for row in conn.execute("""SELECT m.id,m.title,m.completion_criteria,d.due_date
                FROM milestones m LEFT JOIN deadline_versions d ON d.project_id=m.project_id AND d.milestone_id=m.id AND d.status='approved'
                WHERE m.project_id=? ORDER BY m.rowid""", (project_id,))]
        return {"project_id": project_id, "confirmed_at": project["baseline_confirmed_at"], "objectives": objectives, "milestones": milestones}
