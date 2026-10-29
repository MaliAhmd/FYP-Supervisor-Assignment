"""Evidence references are necessary, never sufficient, proof of completion."""
import json

from app.db.repository import new_id
from app.orchestrator.deadlines import DeadlineService


def valid_ids(conn, pid, ids, *, allowed=None, independent=False):
    if not ids or len(set(ids)) != len(ids):
        return False
    for key in ids:
        row = conn.execute('SELECT type FROM evidence WHERE project_id=? AND id=?', (pid, key)).fetchone()
        if row is None or (allowed is not None and key not in allowed):
            return False
        if independent and row[0] not in {'file_diff', 'pdf_page'}:
            return False
    return True


def milestone_valid(conn, pid, mid):
    return mid is None or conn.execute('SELECT 1 FROM milestones WHERE project_id=? AND id=?', (pid, mid)).fetchone() is not None


def save_review(conn, pid, cid, output, allowed, open_feedback):
    for finding in output.findings:
        if not milestone_valid(conn, pid, finding.milestone_id) or not valid_ids(conn, pid, finding.evidence_ids, allowed=allowed):
            raise ValueError('invalid_finding_evidence')
        key = new_id()
        conn.execute('INSERT INTO findings(id,project_id,cycle_id,milestone_id,severity,text,evidence_ids_json) VALUES (?,?,?,?,?,?,?)',
                     (key, pid, cid, finding.milestone_id, finding.severity, finding.text, json.dumps(finding.evidence_ids)))
        if finding.severity != 'info':
            conn.execute("INSERT INTO feedback_items(id,project_id,text,status,opened_cycle,evidence_ids_json) VALUES (?,?,?,'open',?,?)", (new_id(), pid, finding.text, cid, json.dumps(finding.evidence_ids)))
    for check in output.feedback:
        if check.feedback_id not in open_feedback or not valid_ids(conn, pid, check.evidence_ids, allowed=allowed):
            raise ValueError('invalid_feedback_evidence')
        conn.execute("UPDATE feedback_items SET status=?,resolved_cycle=?,evidence_ids_json=? WHERE project_id=? AND id=? AND status!='resolved'",
                     (check.status, cid if check.status == 'resolved' else None, json.dumps(check.evidence_ids), pid, check.feedback_id))


def save_claims(conn, pid, cid, claims, sources, allowed):
    for claim in claims:
        if claim.source_id not in sources or not milestone_valid(conn, pid, claim.milestone_id):
            raise ValueError('invalid_claim_source')
        refs_valid = valid_ids(conn, pid, claim.evidence_ids, allowed=allowed, independent=True)
        # An LLM cannot decide that arbitrary code satisfies arbitrary criteria.
        # Even valid citations remain unverified until a supervisor confirms.
        status = 'unsupported' if claim.completion and not refs_valid else 'unverified'
        reason = 'missing_independent_evidence' if status == 'unsupported' else 'requires_supervisor_verification'
        if claim.completion and refs_valid and claim.milestone_id:
            approved = conn.execute('SELECT evidence_id FROM milestone_completions WHERE project_id=? AND milestone_id=?', (pid, claim.milestone_id)).fetchone()
            if approved and approved[0] in claim.evidence_ids:
                status, reason = 'supported', 'supervisor_confirmed_evidence'
        key = new_id()
        conn.execute('INSERT INTO claims(id,project_id,milestone_id,source_evidence_id,text,status) VALUES (?,?,?,?,?,?)', (key, pid, claim.milestone_id, claim.source_id, claim.text, status))
        conn.execute('INSERT INTO claim_verifications(project_id,claim_id,cycle_id,evidence_ids_json,reason) VALUES (?,?,?,?,?)', (pid, key, cid, json.dumps(claim.evidence_ids if refs_valid else []), reason))
        if claim.due_date:
            if not claim.milestone_id:
                raise ValueError('deadline_requires_milestone')
            DeadlineService.propose_in(conn, pid, claim.milestone_id, claim.due_date, 'email:' + claim.source_id)
