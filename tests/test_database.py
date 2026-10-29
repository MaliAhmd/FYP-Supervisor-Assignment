import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Database, ProjectStore
from app.db.database import MigrationError


EXPECTED_TABLES = {
    "projects", "milestones", "deadline_versions", "push_events", "cycles",
    "checkpoints", "evidence", "reports", "claims", "findings", "feedback_items",
    "email_drafts", "email_inbound", "supervisor_decisions", "register_sync",
    "llm_calls", "tool_calls", "llm_cache", "usage_quota", "audit_log",
    "gateway_jobs", "gateway_model_limits", "github_state", "github_invitations",
    "pdf_documents", "pdf_pages", "baseline_drafts", "project_objectives",
}


def test_schema_wal_foreign_keys_and_project_ownership(db):
    with db.transaction(write=False) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED_TABLES <= tables
        for table in EXPECTED_TABLES - {"projects"}:
            assert "project_id" in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_repeated_and_concurrent_migrations_are_safe(db):
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: db.migrate(), range(8)))
    with db.transaction(write=False) as conn:
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == ["001_initial.sql", "002_gateway.sql", "003_github_polling.sql", "004_pdf_baselines.sql", "005_reviews.sql", "006_email.sql", "007_sheets.sql", "008_ui.sql"]


def test_migration_failure_rolls_back_entire_script(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_bad.sql").write_text("CREATE TABLE example (id TEXT);\nINVALID SQL;\n")
    db = Database(tmp_path / "broken.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(directory)
    with db.transaction(write=False) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='example'").fetchone() is None


def test_changed_applied_migration_rejected(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    path = directory / "001.sql"
    path.write_text("CREATE TABLE example (id TEXT);\n")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate(directory)
    path.write_text("CREATE TABLE example (id INTEGER);\n")
    with pytest.raises(MigrationError, match="Applied migration changed"):
        db.migrate(directory)


def test_transaction_rolls_back(db, project):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("UPDATE projects SET student_name='wrong' WHERE id=?", (project.project_id,))
            raise RuntimeError("abort")
    assert project.project()["student_name"] == "Amina Example"


def test_state_survives_restart_and_cycle_dedupes(db, project):
    cycle = project.create_cycle("sha-one")
    project.save_checkpoint(cycle, "triage", {"files": ["main.py"]})
    evidence = project.add_evidence(cycle, "file_diff", "main.py", "+print('hello')")
    restarted = Database(db.path)
    restarted.migrate()
    store = ProjectStore(restarted, project.project_id)
    assert store.create_cycle("sha-one") == cycle
    assert store.checkpoint(cycle, "triage") == {"files": ["main.py"]}
    assert store.evidence(evidence)["content"] == "+print('hello')"
    assert store.add_evidence(cycle, "file_diff", "main.py", "+print('hello')") == evidence


def test_project_data_cache_evidence_and_checkpoints_isolated(db, project):
    other = ProjectStore.create(db, "Bilal Example", "bilal@example.com", "fictional/bilal")
    cycle = project.create_cycle("sha")
    other_cycle = other.create_cycle("sha")
    assert cycle != other_cycle
    evidence = project.add_evidence(cycle, "email", "mail-1", "Private student evidence")
    project.cache_put("same-hash", "model", {"private": "amina"})
    project.save_checkpoint(cycle, "review", {"private": "amina"})
    assert other.evidence(evidence) is None
    assert other.cache_get("same-hash") is None
    assert other.checkpoint(cycle, "review") is None
    assert all(row["project_id"] == other.project_id for row in other.audit_history())
    other.cache_put("same-hash", "model", {"private": "bilal"})
    assert project.cache_get("same-hash") == {"private": "amina"}
    with pytest.raises(sqlite3.IntegrityError):
        other.save_checkpoint(cycle, "review", {})
    with pytest.raises(ValueError, match="Cycle does not belong"):
        other.add_evidence(cycle, "email", "mail-2", "text")
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("""INSERT INTO findings(id,cycle_id,project_id,severity,text,evidence_ids_json)
                VALUES ('finding',?,?,'warning','invalid',?)""", (other_cycle, other.project_id, json.dumps([evidence])))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("""INSERT INTO claims(id,project_id,source_evidence_id,text)
                VALUES ('claim',?,?,'invalid')""", (other.project_id, evidence))


def test_one_running_cycle_per_project(db, project):
    first = project.create_cycle("sha-1")
    second = project.create_cycle("sha-2")
    with db.transaction() as conn:
        conn.execute("UPDATE cycles SET status='running' WHERE project_id=? AND id=?", (project.project_id, first))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("UPDATE cycles SET status='running' WHERE project_id=? AND id=?", (project.project_id, second))


def test_deadline_schema_preserves_history_and_one_current_version(db, project):
    pid = project.project_id
    with db.transaction() as conn:
        conn.execute("INSERT INTO milestones(id,project_id,title,completion_criteria) VALUES ('m1',?,'Build','Tests pass')", (pid,))
        conn.execute("""INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,source,decided_by)
            VALUES ('d1',?,'m1','2026-11-01','approved','baseline','supervisor')""", (pid,))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("""INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,source,decided_by)
                VALUES ('d2',?,'m1','2026-11-02','approved','email','supervisor')""", (pid,))
    with db.transaction() as conn:
        conn.execute("UPDATE deadline_versions SET status='superseded' WHERE project_id=? AND id='d1'", (pid,))
        conn.execute("""INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,supersedes_id,source,decided_by)
            VALUES ('d2',?,'m1','2026-11-02','approved','d1','email','supervisor')""", (pid,))
        assert conn.execute("SELECT count(*) FROM deadline_versions WHERE project_id=?", (pid,)).fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("DELETE FROM deadline_versions WHERE project_id=?", (pid,))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("UPDATE deadline_versions SET due_date='2026-12-01' WHERE project_id=?", (pid,))


def test_audit_redaction_and_append_only(db, project):
    project.audit("integration.configured", {"password": "do-not-store", "nested": {"access_token": "hidden"}, "status": "ok"})
    history = project.audit_history()
    assert "do-not-store" not in json.dumps(history)
    assert "hidden" not in json.dumps(history)
    assert json.loads(history[-1]["metadata_json"])["status"] == "ok"
    for statement in ["DELETE FROM audit_log WHERE project_id=?", "UPDATE audit_log SET actor='changed' WHERE project_id=?"]:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(statement, (project.project_id,))


def test_supervisor_decisions_cannot_be_overwritten(db, project):
    with db.transaction() as conn:
        conn.execute("""INSERT INTO supervisor_decisions(id,project_id,kind,target_id,decision,actor)
            VALUES ('decision',?,'deadline','d1','approve','supervisor')""", (project.project_id,))
    for statement in ["DELETE FROM supervisor_decisions WHERE project_id=?", "UPDATE supervisor_decisions SET decision='reject' WHERE project_id=?"]:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(statement, (project.project_id,))


def test_email_schema_requires_approval_and_deduplicates(db, project):
    cycle = project.create_cycle("email-cycle")
    values = (project.project_id, cycle, project.project()["student_email"])
    with db.transaction() as conn:
        conn.execute("""INSERT INTO email_drafts(id,project_id,cycle_id,to_addr,subject,body,message_id,idempotency_key)
            VALUES ('draft',?,?,?,'Follow up','Please provide evidence','<draft@example.com>','email-key')""", values)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("UPDATE email_drafts SET status='sent' WHERE project_id=? AND id='draft'", (project.project_id,))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("""INSERT INTO email_drafts(id,project_id,cycle_id,to_addr,subject,body,message_id,idempotency_key)
                VALUES ('duplicate',?,?,?,'Follow up','Duplicate','<different@example.com>','different-key')""", values)


def test_invalid_project_rejected(db):
    from app.db.repository import ProjectNotFound

    with pytest.raises(ProjectNotFound):
        ProjectStore(db, "missing")
    with pytest.raises(ValueError):
        ProjectStore.create(db, "", "a@example.com", "owner/repo")
    with pytest.raises(ValueError):
        ProjectStore.create(db, "Student", "a@example.com", "https://github.com/owner/repo")
