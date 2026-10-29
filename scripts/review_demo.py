"""Offline Phase 5 smoke demo, with a real stop/restart between review stages."""
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agents.fake import FakeReviewClient
from app.agents.review import all_specs
from app.config import Settings
from app.db import Database, ProjectStore
from app.gateway import Gateway
from app.integrations.github.fake import FakeGitHubClient
from app.integrations.pdf.store import PDFStore
from app.orchestrator.review import ReviewEngine


def main():
    with TemporaryDirectory(prefix='supervisorai-review-') as directory:
        db = Database(Path(directory) / 'demo.sqlite3')
        db.migrate()
        config = Settings(_env_file=None, llm_mode='fake', github_mode='fake', email_mode='test',
                          llm_requests_per_minute=100, llm_tokens_per_minute=100000)
        project = ProjectStore.create(db, 'Fictional Student', 'student@example.com', 'fictional/review-demo')
        pid = project.project_id
        # Fixture baseline only; production onboarding uses BaselineService.confirm.
        with db.transaction() as conn:
            conn.execute("UPDATE projects SET status='active',baseline_confirmed_at='2026-01-01' WHERE id=?", (pid,))
            conn.execute("INSERT INTO milestones(id,project_id,title,completion_criteria) VALUES ('demo-m',?,'Prototype','Implement evaluation')", (pid,))
            conn.execute("INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,source,decided_by) VALUES ('demo-d',?,'demo-m','2026-01-01','approved','baseline','supervisor')", (pid,))
        git = FakeGitHubClient()
        git.add_repository('fictional/review-demo')
        head = git.publish('fictional/review-demo', {'main.py': 'def evaluate():\n    return 42\n'})
        cid = project.create_cycle(head)
        with Gateway(db, config, all_specs(), client=FakeReviewClient(), start=False) as gateway:
            engine = ReviewEngine(db, gateway, git, PDFStore(db, config))
            for _ in range(3):
                engine.step(pid, cid, batch_id='demo-batch')
            gateway.run_once()
            print('Stopped with a persisted coordinator result.')
        with Gateway(db, config, all_specs(), client=FakeReviewClient(), start=False) as gateway:
            engine = ReviewEngine(db, gateway, git, PDFStore(db, config))
            for _ in range(30):
                state = engine.step(pid, cid, batch_id='demo-batch')
                if state['status'] in {'completed', 'pending_evidence_review'}:
                    break
                gateway.run_once()
            assert state['status'] == 'completed', state['reason']
            with db.transaction(write=False) as conn:
                assert conn.execute('SELECT count(*) FROM email_drafts WHERE project_id=?', (pid,)).fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM llm_calls WHERE project_id=? AND agent='coordinator' AND attempted=1", (pid,)).fetchone()[0] == 1
            print('Restart completed the review with one pending draft and no repeated coordinator call.')
            print('No email sent; no live services called. Full three-project scenario comes in Phase 9.')


if __name__ == '__main__':
    main()
