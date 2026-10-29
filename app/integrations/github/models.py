"""Small validated GitHub read contracts. No provider error bodies are exposed."""

from dataclasses import dataclass
import re
from typing import Protocol


class GitHubError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None, retry_at: float = 0):
        allowed = {"authentication_required", "access_denied", "not_found", "empty_repository",
                   "rate_limited", "transport_error", "provider_error", "invalid_response",
                   "response_too_large", "incomplete_result", "redirect_requires_review"}
        super().__init__(code if code in allowed else "provider_error")
        self.status, self.retry_at = status, retry_at


def repo_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+", value) or value.split("/")[1] in {".", ".."}:
        raise ValueError("Repository must be owner/repo")
    return value


def sha_value(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", value):
        raise ValueError("Expected a full Git commit/blob SHA")
    return value.lower()


def branch_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid branch name")
    if value.startswith(("/", "-")) or value.endswith(("/", ".", ".lock")) or any(s in value for s in ("..", "@{", "//", "\\", " ", "~", "^", ":", "?", "*", "[")):
        raise ValueError("Invalid branch name")
    return value


@dataclass(frozen=True)
class Repository:
    full_name: str
    default_branch: str
    can_read: bool = True


@dataclass(frozen=True)
class Invitation:
    id: int
    repo_full_name: str
    permissions: str


@dataclass(frozen=True)
class Head:
    sha: str | None
    etag: str | None = None
    not_modified: bool = False
    poll_interval: float = 0


@dataclass(frozen=True)
class ChangeSet:
    mode: str
    after_sha: str
    files: tuple[dict, ...]
    history_rewritten: bool = False
    reason: str | None = None


class GitHubReader(Protocol):
    def repository(self, repo: str) -> Repository: ...
    def invitations(self) -> list[Invitation]: ...
    def head(self, repo: str, branch: str, etag: str | None = None) -> Head: ...
    def compare(self, repo: str, before: str, after: str) -> dict: ...
    def commit(self, repo: str, sha: str) -> dict: ...
    def tree(self, repo: str, sha: str) -> list[dict]: ...
    def blob(self, repo: str, sha: str) -> bytes: ...
    def close(self) -> None: ...
