"""Read-only change preparation. Review/triage belongs to Phase 5."""

from app.integrations.github.models import ChangeSet, GitHubError, GitHubReader, sha_value


def read_changes(reader: GitHubReader, repo: str, before: str | None, after: str) -> ChangeSet:
    after = sha_value(after)
    reason, rewritten = "initial_tree", False
    if before:
        before = sha_value(before)
        try:
            compared = reader.compare(repo, before, after)
        except GitHubError as error:
            if error.status not in {404, 422}:
                raise
            reason, rewritten = "history_unavailable", True
        else:
            rewritten = compared["status"] in {"behind", "diverged"} or compared["merge_base_commit"]["sha"] != before
            if rewritten:
                reason = "history_rewritten"
            elif len(compared["files"]) >= 300:
                # GitHub compare returns at most 300 changed files, even with pagination.
                reason = "diff_file_limit"
            else:
                return ChangeSet("diff", after, tuple(compared["files"]))
    commit = reader.commit(repo, after)
    tree = reader.tree(repo, commit["tree"]["sha"])
    return ChangeSet("tree", after, tuple(tree), rewritten, reason)
