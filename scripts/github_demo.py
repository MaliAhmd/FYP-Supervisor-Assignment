"""Phase 3 fake invitation/polling/debounce/restart demo; never contacts GitHub."""

import argparse
import json
from pathlib import Path
import tempfile

from app.config import Settings
from app.db import Database, ProjectStore
from app.integrations.github.access import AccessService
from app.integrations.github.changes import read_changes
from app.integrations.github.fake import FakeGitHubClient
from app.scheduler.polling import PollingTrigger


class DemoClock:
    def __init__(self):
        self.now = 1792886400.0

    def __call__(self):
        return self.now


def run_demo(path: Path) -> dict:
    config = Settings(_env_file=None, llm_mode="fake", github_mode="fake", email_mode="test", database_path=path)
    db, clock = Database(config.database_path), DemoClock()
    db.migrate()
    project = ProjectStore.create(db, "Fictional Phase 3 Student", "phase3@example.com", "fictional/phase3-demo")
    with db.transaction() as conn:
        # This fixture stands in for Phase 4 supervisor-confirmed onboarding.
        conn.execute("UPDATE projects SET baseline_confirmed_at='2026-10-25T00:00:00Z' WHERE id=?", (project.project_id,))
    fake = FakeGitHubClient()
    repo = project.project()["repo_full_name"]
    fake.add_repository(repo, accessible=False)
    first_sha = fake.publish(repo, {"main.py": "print('first draft')"})
    fake.invite(repo, 101)
    access = AccessService(db, fake, fake.accept_invitation, clock=clock)
    access.discover(project.project_id)
    access.accept(project.project_id, 101)
    trigger = PollingTrigger(db, config, fake, clock=clock)
    trigger.tick()
    clock.now += 60
    unchanged = trigger.poll_project(project.project_id)
    clock.now += 60
    latest = fake.publish(repo, {"main.py": "print('second draft')", "README.md": "Demo"})
    trigger.tick()
    assert not trigger.queue.pending_cycles(project.project_id)
    # A fresh DB/service object resumes saved cursors. Fake remote remains the same
    # fixture, just as a real remote remains available across application restart.
    restarted_db = Database(db.path)
    restarted_db.migrate()
    restarted = PollingTrigger(restarted_db, config, fake, clock=clock)
    clock.now += config.debounce_seconds
    restarted.tick()
    jobs = restarted.queue.pending_cycles(project.project_id)
    assert len(jobs) == 1 and jobs[0]["after_sha"] == latest
    assert project.project()["last_reviewed_sha"] is None
    replacement = fake.publish(repo, {"replacement.py": "print('new history')"}, rewritten=True)
    changes = read_changes(fake, repo, first_sha, replacement)
    assert changes.history_rewritten and changes.mode == "tree"
    with db.transaction(write=False) as conn:
        events = conn.execute("SELECT count(*) FROM push_events WHERE project_id=?", (project.project_id,)).fetchone()[0]
    return {"project_id": project.project_id, "access": project.project()["status"],
            "unchanged_poll": unchanged, "push_events": events, "review_jobs": len(jobs),
            "queued_after_sha": jobs[0]["after_sha"], "restart_preserved_state": True,
            "force_push_mode": changes.mode, "history_rewritten": changes.history_rewritten,
            "note": "Review job queued only; Phase 5 will execute reviews."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Optional persistent demo database (adds a fictional project per run)")
    args = parser.parse_args()
    if args.database:
        result = run_demo(args.database)
    else:
        with tempfile.TemporaryDirectory(prefix="supervisorai-github-demo-") as directory:
            result = run_demo(Path(directory) / "demo.sqlite3")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
