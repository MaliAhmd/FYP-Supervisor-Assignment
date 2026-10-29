"""Deterministic in-memory GitHub fixture. Publishing changes affects no real repo."""

from copy import deepcopy
import difflib
from hashlib import sha1
import json

from app.integrations.github.models import GitHubError, Head, Invitation, Repository, repo_name


class FakeGitHubClient:
    def __init__(self):
        self.repos = {}
        self.pending_invitations = {}
        self.requests = []
        self.accepted_ids = []

    def add_repository(self, repo: str, *, branch="main", accessible=True):
        self.repos[repo_name(repo)] = {"branch": branch, "accessible": accessible,
                                      "head": None, "commits": {}, "trees": {}, "blobs": {}}

    def invite(self, repo: str, invitation_id: int):
        self.pending_invitations[invitation_id] = Invitation(invitation_id, repo, "write")

    def accept_invitation(self, invitation_id: int):
        invitation = self.pending_invitations.pop(invitation_id, None)
        if invitation is None:
            raise GitHubError("not_found", status=404)
        self.repos[invitation.repo_full_name]["accessible"] = True
        self.accepted_ids.append(invitation_id)

    def publish(self, repo: str, files: dict[str, str | bytes], *, rewritten=False) -> str:
        state = self.repos[repo]
        entries = []
        for path, value in sorted(files.items()):
            content = value.encode() if isinstance(value, str) else value
            blob_sha = sha1(content).hexdigest()
            state["blobs"][blob_sha] = content
            entries.append({"path": path, "type": "blob", "mode": "100644", "sha": blob_sha, "size": len(content)})
        tree_sha = sha1(json.dumps(entries, sort_keys=True).encode()).hexdigest()
        state["trees"][tree_sha] = entries
        commit_sha = sha1(f"{repo}:{tree_sha}:{len(state['commits'])}".encode()).hexdigest()
        state["commits"][commit_sha] = {"sha": commit_sha, "tree": {"sha": tree_sha},
                                         "parent": None if rewritten else state["head"]}
        state["head"] = commit_sha
        return commit_sha

    def _repo(self, repo):
        if repo not in self.repos or not self.repos[repo]["accessible"]:
            raise GitHubError("not_found", status=404)
        return self.repos[repo]

    def repository(self, repo: str) -> Repository:
        self.requests.append(("GET", "/repos/" + repo, None))
        state = self._repo(repo)
        return Repository(repo, state["branch"])

    def invitations(self) -> list[Invitation]:
        self.requests.append(("GET", "/user/repository_invitations", None))
        return list(self.pending_invitations.values())

    def head(self, repo: str, branch: str, etag: str | None = None) -> Head:
        self.requests.append(("GET", f"/repos/{repo}/commits/{branch}", etag))
        state = self._repo(repo)
        if not state["head"]:
            raise GitHubError("empty_repository", status=409)
        if branch != state["branch"]:
            raise GitHubError("not_found", status=404)
        current = '"' + state["head"] + '"'
        return Head(None if etag == current else state["head"], current, etag == current)

    def commit(self, repo: str, sha: str) -> dict:
        self.requests.append(("GET", f"/repos/{repo}/git/commits/{sha}", None))
        try:
            return deepcopy(self._repo(repo)["commits"][sha])
        except KeyError:
            raise GitHubError("not_found", status=404) from None

    def tree(self, repo: str, sha: str) -> list[dict]:
        self.requests.append(("GET", f"/repos/{repo}/git/trees/{sha}", None))
        try:
            return deepcopy(self._repo(repo)["trees"][sha])
        except KeyError:
            raise GitHubError("not_found", status=404) from None

    def blob(self, repo: str, sha: str) -> bytes:
        self.requests.append(("GET", f"/repos/{repo}/git/blobs/{sha}", None))
        try:
            return self._repo(repo)["blobs"][sha]
        except KeyError:
            raise GitHubError("not_found", status=404) from None

    def compare(self, repo: str, before: str, after: str) -> dict:
        self.requests.append(("GET", f"/repos/{repo}/compare/{before}...{after}", None))
        state = self._repo(repo)
        def ancestors(sha):
            result = []
            while sha:
                if sha not in state["commits"]:
                    raise GitHubError("not_found", status=404)
                result.append(sha)
                sha = state["commits"][sha]["parent"]
            return result
        old_chain, new_chain = ancestors(before), ancestors(after)
        status = "identical" if before == after else "ahead" if before in new_chain else "behind" if after in old_chain else "diverged"
        common = next((sha for sha in new_chain if sha in old_chain), after)
        old = {item["path"]: item for item in state["trees"][state["commits"][before]["tree"]["sha"]]}
        new = {item["path"]: item for item in state["trees"][state["commits"][after]["tree"]["sha"]]}
        files = []
        for path in sorted(old.keys() | new.keys()):
            previous, current = old.get(path), new.get(path)
            if previous == current:
                continue
            old_bytes = state["blobs"][previous["sha"]] if previous else b""
            new_bytes = state["blobs"][current["sha"]] if current else b""
            file = {"filename": path, "sha": (current or previous)["sha"],
                    "status": "added" if not previous else "removed" if not current else "modified"}
            try:
                file["patch"] = "\n".join(difflib.unified_diff(old_bytes.decode().splitlines(), new_bytes.decode().splitlines(), fromfile=path, tofile=path))
            except UnicodeDecodeError:
                pass
            files.append(file)
        return {"status": status, "merge_base_commit": {"sha": common}, "files": files[:300]}

    def close(self):
        pass
