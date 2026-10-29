"""Supervisor-only decisions; student proposals never change effective dates."""
from datetime import date
import json

from app.db.repository import new_id


def iso_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Expected ISO date')
    return value


class DeadlineService:
    def __init__(self, db):
        self.db = db

    def propose(self, project_id, milestone_id, due_date, *, source):
        with self.db.transaction() as conn:
            return self.propose_in(conn, project_id, milestone_id, due_date, source)

    @staticmethod
    def propose_in(conn, pid, mid, due, source):
        iso_date(due)
        if not source or not conn.execute('SELECT 1 FROM milestones WHERE project_id=? AND id=?', (pid, mid)).fetchone():
            raise ValueError('Invalid proposal')
        existing = conn.execute('SELECT id FROM deadline_versions WHERE project_id=? AND milestone_id=? AND due_date=? AND source=?', (pid, mid, due, source)).fetchone()
        if existing:
            return existing[0]
        old = conn.execute("SELECT id FROM deadline_versions WHERE project_id=? AND milestone_id=? AND status='approved'", (pid, mid)).fetchone()
        key = new_id()
        conn.execute("INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,supersedes_id,source) VALUES (?,?,?,?,'proposed',?,?)", (key, pid, mid, due, old[0] if old else None, source))
        return key

    def decide(self, project_id, version_id, *, approve: bool):
        with self.db.transaction() as conn:
            row = conn.execute('SELECT * FROM deadline_versions WHERE project_id=? AND id=?', (project_id, version_id)).fetchone()
            if not row or row['status'] != 'proposed':
                raise ValueError('Expected a proposed deadline')
            current = conn.execute("SELECT id FROM deadline_versions WHERE project_id=? AND milestone_id=? AND status='approved'", (project_id, row['milestone_id'])).fetchone()
            if approve and (current[0] if current else None) != row['supersedes_id']:
                raise ValueError('Stale proposal: propose again against the current deadline')
            if approve and current:
                conn.execute("UPDATE deadline_versions SET status='superseded' WHERE project_id=? AND id=?", (project_id, current[0]))
            status = 'approved' if approve else 'rejected'
            conn.execute("UPDATE deadline_versions SET status=?,decided_by='supervisor' WHERE project_id=? AND id=?", (status, project_id, version_id))
            conn.execute("INSERT INTO supervisor_decisions(id,project_id,kind,target_id,decision,actor) VALUES (?,?,'deadline',?,?,'supervisor')", (new_id(), project_id, version_id, status))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'deadline.decided','supervisor',?)", (project_id, json.dumps({'id': version_id, 'status': status})))

    def confirm_completion(self, project_id, milestone_id, evidence_id):
        with self.db.transaction() as conn:
            item = conn.execute("SELECT type FROM evidence WHERE project_id=? AND id=?", (project_id, evidence_id)).fetchone()
            if not item or item[0] not in {'file_diff', 'pdf_page'}:
                raise ValueError('Completion needs independent project evidence')
            conn.execute("INSERT INTO milestone_completions(project_id,milestone_id,evidence_id,actor) VALUES (?,?,?,'supervisor') ON CONFLICT(project_id,milestone_id) DO UPDATE SET evidence_id=excluded.evidence_id", (project_id, milestone_id, evidence_id))
            conn.execute("INSERT INTO supervisor_decisions(id,project_id,kind,target_id,decision,actor,payload_json) VALUES (?,?,'completion',?,'confirmed','supervisor',?)", (new_id(), project_id, milestone_id, json.dumps({'evidence_id': evidence_id})))

    def delayed(self, project_id, *, today=None):
        today = today or date.today()
        with self.db.transaction(write=False) as conn:
            return [dict(r) for r in conn.execute("""SELECT m.id,m.title,d.due_date FROM milestones m
                JOIN deadline_versions d ON d.project_id=m.project_id AND d.milestone_id=m.id AND d.status='approved'
                WHERE m.project_id=? AND d.due_date<? AND NOT EXISTS
                (SELECT 1 FROM milestone_completions c WHERE c.project_id=m.project_id AND c.milestone_id=m.id)""", (project_id, today.isoformat()))]
