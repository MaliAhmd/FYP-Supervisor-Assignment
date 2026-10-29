"""Supervisor-invoked invitation decisions, followed by read-access verification."""

import json
import time

from app.db import Database, ProjectStore
from app.integrations.github.models import GitHubError, GitHubReader, branch_name
from app.scheduler.queue import timestamp


class AccessService:
    def __init__(self, db: Database, reader: GitHubReader, accept_invitation, *, clock=time.time):
        self.db, self.reader, self.accept_invitation, self.clock = db, reader, accept_invitation, clock

    def discover(self, project_id: str) -> list[dict]:
        project = ProjectStore(self.db, project_id).project()
        matches = [invitation for invitation in self.reader.invitations()
                   if invitation.repo_full_name.lower() == project["repo_full_name"].lower()]
        with self.db.transaction() as conn:
            conn.execute("UPDATE github_invitations SET status='unavailable' WHERE project_id=? AND status='pending'", (project_id,))
            for invitation in matches:
                conn.execute("""INSERT INTO github_invitations(project_id,invitation_id,permissions,status,discovered_at)
                    VALUES (?,?,?,'pending',?) ON CONFLICT(project_id,invitation_id) DO UPDATE SET
                    status='pending',permissions=excluded.permissions""",
                             (project_id, invitation.id, invitation.permissions, timestamp(self.clock())))
            return [dict(row) for row in conn.execute("SELECT * FROM github_invitations WHERE project_id=? AND status='pending'", (project_id,))]

    def accept(self, project_id: str, invitation_id: int) -> str:
        if type(invitation_id) is not int or invitation_id <= 0:
            raise ValueError("Invalid invitation ID")
        project = ProjectStore(self.db, project_id).project()
        if project["status"] == "paused":
            raise ValueError("Resume the project before accepting an invitation")
        if not project["baseline_confirmed_at"]:
            raise ValueError("Confirm the baseline first")
        # Re-read GitHub's list so a stale UI or another project's ID cannot cause a write.
        matches = self.discover(project_id)
        if invitation_id not in {item["invitation_id"] for item in matches}:
            raise ValueError("Invitation does not belong to this project's pending invitations")
        current = ProjectStore(self.db, project_id).project()
        if current["status"] == "paused" or current["repo_full_name"] != project["repo_full_name"] or not current["baseline_confirmed_at"]:
            raise ValueError("Project changed before invitation acceptance")
        self.accept_invitation(invitation_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE github_invitations SET status='accepted',accepted_at=? WHERE project_id=? AND invitation_id=?",
                         (timestamp(self.clock()), project_id, invitation_id))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'github.invitation_accepted','supervisor',?)",
                         (project_id, json.dumps({"invitation_id": invitation_id})))
        # If verification fails, accepted state persists and status stays pending_access.
        # refresh_access is safe to retry without repeating the PATCH.
        return self.refresh_access(project_id)

    def refresh_access(self, project_id: str, *, branch: str | None = None) -> str:
        project = ProjectStore(self.db, project_id).project()
        if project["status"] == "paused":
            raise ValueError("Paused projects require explicit resume")
        if not project["baseline_confirmed_at"]:
            raise ValueError("Confirm the baseline first")
        repository = self.reader.repository(project["repo_full_name"])
        if not repository.can_read:
            raise GitHubError("access_denied")
        with self.db.transaction(write=False) as conn:
            state = conn.execute("SELECT tracked_branch FROM github_state WHERE project_id=?", (project_id,)).fetchone()
        selected = branch_name(branch or (state[0] if state else None) or repository.default_branch)
        try:
            self.reader.head(project["repo_full_name"], selected)
        except GitHubError as error:
            if str(error) != "empty_repository":
                raise
        with self.db.transaction() as conn:
            # Reject activation after a concurrent pause/repository change.
            current = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if current["status"] == "paused" or current["repo_full_name"] != project["repo_full_name"]:
                raise ValueError("Project changed during access verification")
            conn.execute("UPDATE projects SET status='active',etag=NULL WHERE id=?", (project_id,))
            conn.execute("""INSERT INTO github_state(project_id,tracked_branch) VALUES (?,?)
                ON CONFLICT(project_id) DO UPDATE SET tracked_branch=excluded.tracked_branch,next_poll_at=0,
                last_error=NULL,consecutive_errors=0,lease_owner=NULL,lease_until=0""", (project_id, selected))
            conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'github.access_verified','supervisor',?)",
                         (project_id, json.dumps({"branch": selected})))
        return "active"
