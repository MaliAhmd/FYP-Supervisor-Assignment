"""Deterministic supervisor register from a consistent SQLite snapshot."""
from datetime import date

HEADERS = ['Project ID', 'Student', 'Student email', 'Repository', 'Project status',
           'Milestones completed', 'Next approved deadline', 'Delayed milestones',
           'Open feedback', 'Unsupported claims', 'Latest review', 'Last reviewed SHA']


def register_rows(db, *, today=None):
    today = (today or date.today()).isoformat()
    result = {}
    with db.transaction(write=False) as conn:
        for project in conn.execute('SELECT * FROM projects ORDER BY created_at,id'):
            pid = project['id']
            def scalar(sql):
                return conn.execute(sql, (pid,)).fetchone()[0]
            total = scalar('SELECT count(*) FROM milestones WHERE project_id=?')
            complete = scalar('SELECT count(*) FROM milestone_completions WHERE project_id=?')
            due = scalar("""SELECT min(d.due_date) FROM deadline_versions d WHERE d.project_id=? AND d.status='approved'
                AND NOT EXISTS (SELECT 1 FROM milestone_completions c WHERE c.project_id=d.project_id AND c.milestone_id=d.milestone_id)""")
            delayed = conn.execute("""SELECT count(*) FROM deadline_versions d WHERE d.project_id=? AND d.status='approved' AND d.due_date<?
                AND NOT EXISTS (SELECT 1 FROM milestone_completions c WHERE c.project_id=d.project_id AND c.milestone_id=d.milestone_id)""", (pid, today)).fetchone()[0]
            feedback = scalar("SELECT count(*) FROM feedback_items WHERE project_id=? AND status!='resolved'")
            claims = scalar("SELECT count(*) FROM claims WHERE project_id=? AND status='unsupported'")
            cycle = conn.execute("SELECT status,stage FROM cycles WHERE project_id=? AND trigger!='proposal_upload' ORDER BY rowid DESC LIMIT 1", (pid,)).fetchone()
            result[pid] = [pid, project['student_name'], project['student_email'], project['repo_full_name'], project['status'],
                           f'{complete}/{total}', due or '', str(delayed), str(feedback), str(claims),
                           f"{cycle['status']} / {cycle['stage']}" if cycle else '', project['last_reviewed_sha'] or '']
    return result
