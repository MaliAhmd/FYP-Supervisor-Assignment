"""Project-scoped, content-addressed PDF records and page evidence."""

from hashlib import sha256
import json
import re
import threading
from uuid import uuid4

from app.config import Settings
from app.db import Database, ProjectStore
from app.integrations.pdf.extraction import PDFError, extract_pages


class PDFStore:
    def __init__(self, db: Database, settings: Settings, *, extractor=extract_pages):
        self.db, self.settings, self.extractor = db, settings, extractor
        self._ingest_lock = threading.Lock()

    def get(self, project_id: str, document_id: str) -> dict:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("""SELECT id,project_id,cycle_id,kind,filename,sha256,page_count,text_chars,status,report_id
                FROM pdf_documents WHERE project_id=? AND id=?""", (project_id, document_id)).fetchone()
        if row is None:
            raise LookupError("Document not found for project")
        return dict(row)

    def bytes(self, project_id: str, document_id: str) -> bytes:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT pdf_bytes FROM pdf_documents WHERE project_id=? AND id=?", (project_id, document_id)).fetchone()
        if row is None:
            raise LookupError("Document not found for project")
        return row[0]

    def pages(self, project_id: str, document_id: str) -> list[dict]:
        self.get(project_id, document_id)
        with self.db.transaction(write=False) as conn:
            return [dict(row) for row in conn.execute("""SELECT p.page_number,p.evidence_id,e.content,e.ref,e.content_hash
                FROM pdf_pages p JOIN evidence e ON e.project_id=p.project_id AND e.id=p.evidence_id
                WHERE p.project_id=? AND p.document_id=? ORDER BY p.page_number""", (project_id, document_id))]

    def ingest(self, project_id: str, content: bytes, *, kind: str, filename: str = "upload.pdf", cycle_id: str | None = None) -> dict:
        if kind not in {"proposal", "report"}:
            raise ValueError("Unknown PDF kind")
        if not isinstance(content, bytes) or len(content) > self.settings.max_upload_bytes:
            raise PDFError("upload_too_large")
        ProjectStore(self.db, project_id)
        digest = sha256(content).hexdigest()
        # Serialize repeated uploads through this shared store; the transaction below
        # also prevents duplicate documents/versions across separate instances.
        with self._ingest_lock:
            with self.db.transaction(write=False) as conn:
                if cycle_id and conn.execute("SELECT id FROM cycles WHERE project_id=? AND id=?", (project_id, cycle_id)).fetchone() is None:
                    raise ValueError("Cycle does not belong to this project")
                existing = conn.execute("SELECT id FROM pdf_documents WHERE project_id=? AND kind=? AND sha256=?", (project_id, kind, digest)).fetchone()
                parsed = conn.execute("SELECT id FROM pdf_documents WHERE project_id=? AND sha256=? LIMIT 1", (project_id, digest)).fetchone()
            if existing:
                return self.get(project_id, existing[0])
            # The same physical PDF may be attached as both proposal and report.
            pages = ([{"page": p["page_number"], "text": p["content"]} for p in self.pages(project_id, parsed[0])]
                     if parsed else self.extractor(content, self.settings))
            filename = re.sub(r"[\x00-\x1f\x7f]", "", filename.replace("\\", "/").split("/")[-1])[:200] or "upload.pdf"
            document_id, report_id = uuid4().hex, uuid4().hex if kind == "report" else None
            total = sum(len(page["text"]) for page in pages)
            with self.db.transaction() as conn:
                existing = conn.execute("SELECT id FROM pdf_documents WHERE project_id=? AND kind=? AND sha256=?", (project_id, kind, digest)).fetchone()
                if existing:
                    document_id = existing[0]
                else:
                    if kind == "proposal" and conn.execute("SELECT baseline_confirmed_at FROM projects WHERE id=?", (project_id,)).fetchone()[0]:
                        raise ValueError("Baseline is already confirmed")
                    if cycle_id is None:
                        head = f"{kind}:{digest}"
                        key = sha256(json.dumps([project_id, head]).encode()).hexdigest()
                        conn.execute("""INSERT INTO cycles(id,project_id,trigger,after_sha,idempotency_key)
                            VALUES (?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING""",
                                     (uuid4().hex, project_id, f"{kind}_upload", head, key))
                        cycle_id = conn.execute("SELECT id FROM cycles WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()[0]
                    if report_id:
                        legacy = conn.execute("SELECT id FROM reports WHERE project_id=? AND sha256=?", (project_id, digest)).fetchone()
                        if legacy:
                            report_id = legacy[0]
                            conn.execute("UPDATE reports SET path=? WHERE project_id=? AND id=?", (f"sqlite:pdf_documents/{document_id}", project_id, report_id))
                        else:
                            version = conn.execute("SELECT coalesce(max(version),0)+1 FROM reports WHERE project_id=?", (project_id,)).fetchone()[0]
                            conn.execute("INSERT INTO reports(id,project_id,path,sha256,version,page_refs) VALUES (?,?,?,?,?,'[]')",
                                         (report_id, project_id, f"sqlite:pdf_documents/{document_id}", digest, version))
                    conn.execute("""INSERT INTO pdf_documents(id,project_id,cycle_id,kind,filename,sha256,pdf_bytes,page_count,text_chars,status,report_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                 (document_id, project_id, cycle_id, kind, filename, digest, content, len(pages), total, "ready" if total else "no_text", report_id))
                    refs = []
                    for page in pages:
                        evidence_id = uuid4().hex
                        ref = f"pdf:{document_id}#page={page['page']}"
                        conn.execute("""INSERT INTO evidence(id,project_id,cycle_id,type,ref,content_hash,content,meta_json)
                            VALUES (?,?,?,'pdf_page',?,?,?,?)""",
                                     (evidence_id, project_id, cycle_id, ref, sha256(page["text"].encode()).hexdigest(), page["text"],
                                      json.dumps({"document_id": document_id, "page": page["page"], "pdf_sha256": digest})))
                        conn.execute("INSERT INTO pdf_pages(project_id,document_id,page_number,evidence_id) VALUES (?,?,?,?)",
                                     (project_id, document_id, page["page"], evidence_id))
                        refs.append({"page": page["page"], "evidence_id": evidence_id, "ref": ref})
                    if report_id:
                        conn.execute("UPDATE reports SET page_refs=? WHERE project_id=? AND id=?", (json.dumps(refs), project_id, report_id))
                    conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'pdf.ingested','application',?)",
                                 (project_id, json.dumps({"document_id": document_id, "kind": kind, "page_count": len(pages)})))
            return self.get(project_id, document_id)
