"""Offline Phase 4 proposal extraction, supervisor edit, report, and restart demo."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import tempfile

from app.agents.baseline import baseline_spec
from app.config import Settings
from app.db import Database, ProjectStore
from app.gateway import Gateway
from app.gateway.clients import FakeLLMClient
from app.integrations.pdf.retrieval import Retriever
from app.integrations.pdf.store import PDFStore
from app.orchestrator.baseline import BaselineService
from scripts.pdf_fixtures import text_pdf


def run_demo(path: Path) -> dict:
    config = Settings(_env_file=None, database_path=path, llm_mode="fake", github_mode="fake", email_mode="test")
    db = Database(config.database_path)
    db.migrate()
    project = ProjectStore.create(db, "Fictional Phase 4 Student", "phase4@example.com", "fictional/phase4-demo")
    pdfs = PDFStore(db, config)
    # Explicit fake fixtures are intentionally independent of the PDF's instructions.
    response = {"objectives": ["Build a searchable archive"], "milestones": [
        {"title": "Prototype", "completion_criteria": "Search tests pass", "deadline": "2026-12-01", "source_pages": [1]},
        {"title": "Evaluation", "completion_criteria": "Report precision and recall", "deadline": "2026-12-20", "source_pages": [1]},
    ], "warnings": []}
    content = text_pdf(["Objective: Build a searchable archive.\nPrototype: search tests pass, deadline 2026-12-01.\nEvaluation: report precision and recall, deadline 2026-12-20."])
    fake = FakeLLMClient(responses={"baseline": response})
    with Gateway(db, config, [baseline_spec()], client=fake) as gateway:
        service = BaselineService(db, pdfs, gateway)
        draft = service.upload(project.project_id, content, filename="proposal.pdf")
        queued = service.request_extraction(project.project_id, draft["id"], "baseline-demo-batch")
        if queued["job_id"] is None:
            raise RuntimeError("Demo extraction could not be queued")
        gateway.wait(project.project_id, queued["job_id"])
        draft = service.collect(project.project_id, draft["id"])
        if draft["status"] != "editable":
            raise RuntimeError("Demo extraction did not produce an editable draft")
        edited = deepcopy(draft["draft"])
        edited["milestones"][0]["deadline"] = "2026-12-05"
        draft = service.edit(project.project_id, draft["id"], edited, expected_revision=draft["revision"])
        saved = service.confirm(project.project_id, draft["id"], expected_revision=draft["revision"],
                                acknowledge_limited_coverage=bool(draft["limited_coverage"]))
    # No gateway or uploaded proposal is needed to load confirmed memory.
    restarted_db = Database(db.path)
    restarted_db.migrate()
    restarted_store = PDFStore(restarted_db, config)
    assert BaselineService(restarted_db, restarted_store).baseline(project.project_id) == saved
    report = text_pdf(["Architecture: archive and index.", "Evaluation: precision and recall measurements."])
    first = restarted_store.ingest(project.project_id, report, kind="report", filename="report.pdf")
    duplicate = restarted_store.ingest(project.project_id, report, kind="report", filename="report-copy.pdf")
    revised = restarted_store.ingest(project.project_id, text_pdf(["Architecture: archive and index.", "Evaluation: precision and recall with additional test cases."]), kind="report", filename="report.pdf")
    matches = Retriever(restarted_store).retrieve(project.project_id, revised["id"], "evaluation precision recall")
    assert first["id"] == duplicate["id"] and first["sha256"] != revised["sha256"]
    assert len(fake.requests) == 1
    return {"project_id": project.project_id, "baseline": saved, "llm_calls": len(fake.requests),
            "restart_preserved_baseline": True, "duplicate_report_reused": first["id"] == duplicate["id"],
            "report_revision_stored": True, "retrieved_pages": [p.page for p in matches],
            "access_status": project.project()["status"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Optional persistent demo DB; adds a fictional project each run")
    args = parser.parse_args()
    if args.database:
        result = run_demo(args.database)
    else:
        with tempfile.TemporaryDirectory(prefix="supervisorai-baseline-demo-") as directory:
            result = run_demo(Path(directory) / "demo.sqlite3")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
