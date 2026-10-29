"""GET-only GitHub REST client. The sole write lives in invitations.py."""

import base64
import binascii
import math
import time
from urllib.parse import quote

import httpx

from app.config import Settings
from app.gateway.limiter import retry_after_seconds
from app.integrations.github.models import GitHubError, Head, Invitation, Repository, branch_name, repo_name, sha_value


API_ROOT = "https://api.github.com"


def auth_headers(settings: Settings) -> dict:
    if not settings.github_token.get_secret_value():
        raise ValueError("GITHUB_TOKEN is required")
    return {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + settings.github_token.get_secret_value(),
            "X-GitHub-Api-Version": settings.github_api_version}


def check_response(response: httpx.Response, now: float, allowed=(200,)) -> None:
    status = response.status_code
    if status in allowed:
        return
    headers = response.headers
    secondary_limit = False
    if status == 403:
        try:
            message = response.json().get("message", "").lower()
            secondary_limit = "rate limit" in message
        except (ValueError, AttributeError):
            pass
    if status == 429 or (status == 403 and (secondary_limit or headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)):
        delay = max(60 if "retry-after" not in headers else 1, retry_after_seconds(headers.get("retry-after"), now))
        try:
            if headers.get("x-ratelimit-remaining") == "0":
                reset = float(headers.get("x-ratelimit-reset", 0))
                if math.isfinite(reset):
                    delay = max(delay, reset - now)
        except ValueError:
            pass
        raise GitHubError("rate_limited", status=status, retry_at=now + delay)
    codes = {401: "authentication_required", 403: "access_denied", 404: "not_found", 409: "empty_repository"}
    raise GitHubError("redirect_requires_review" if 300 <= status < 400 else codes.get(status, "provider_error"), status=status)


class GitHubClient:
    def __init__(self, settings: Settings, *, transport=None, clock=time.time):
        self.settings, self.clock = settings, clock
        self._http = httpx.Client(headers=auth_headers(settings), timeout=settings.request_timeout_seconds,
                                  follow_redirects=False, transport=transport)

    def _get(self, path: str, *, params=None, headers=None, allowed=(200,)):
        # All paths are assembled internally; never follow untrusted Link/download URLs.
        try:
            response = self._http.get(API_ROOT + path, params=params, headers=headers)
        except httpx.HTTPError:
            raise GitHubError("transport_error") from None
        check_response(response, self.clock(), allowed)
        if len(response.content) > self.settings.github_max_response_bytes:
            raise GitHubError("response_too_large")
        return response

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            raise GitHubError("invalid_response") from None

    def repository(self, repo: str) -> Repository:
        data = self._json(self._get("/repos/" + repo_name(repo)))
        try:
            name = repo_name(data["full_name"])
            if name.lower() != repo.lower():
                raise ValueError()
            return Repository(name, branch_name(data["default_branch"]), data.get("permissions", {}).get("pull", True) is True)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GitHubError("invalid_response") from None

    def invitations(self) -> list[Invitation]:
        result = []
        for page in range(1, self.settings.github_max_pages + 1):
            response = self._get("/user/repository_invitations", params={"per_page": 100, "page": page})
            data = self._json(response)
            try:
                if not isinstance(data, list):
                    raise ValueError()
                for item in data:
                    if item.get("expired", False):
                        continue
                    invitation_id = item["id"]
                    if type(invitation_id) is not int or invitation_id <= 0:
                        raise ValueError()
                    result.append(Invitation(invitation_id, repo_name(item["repository"]["full_name"]), str(item.get("permissions", "unknown"))))
            except (ValueError, KeyError, TypeError, AttributeError):
                raise GitHubError("invalid_response") from None
            if "next" not in response.links:
                return result
        raise GitHubError("incomplete_result")

    def head(self, repo: str, branch: str, etag: str | None = None) -> Head:
        response = self._get("/repos/" + repo_name(repo) + "/commits/" + quote(branch_name(branch), safe=""),
                             headers={"Accept": "application/vnd.github.sha", **({"If-None-Match": etag} if etag else {})}, allowed=(200, 304))
        try:
            interval = max(0, float(response.headers.get("x-poll-interval", 0)))
            if interval != interval or interval == float("inf"):
                interval = 0
            if response.status_code == 304:
                if not etag:
                    raise ValueError()
                return Head(None, response.headers.get("etag", etag), True, interval)
            return Head(sha_value(response.text.strip()), response.headers.get("etag"), False, interval)
        except (ValueError, KeyError, TypeError):
            raise GitHubError("invalid_response") from None

    def compare(self, repo: str, before: str, after: str) -> dict:
        data = self._json(self._get(f"/repos/{repo_name(repo)}/compare/{sha_value(before)}...{sha_value(after)}",
                                   params={"per_page": 1, "page": 1}))
        try:
            if data["status"] not in {"ahead", "behind", "diverged", "identical"} or not isinstance(data["files"], list):
                raise ValueError()
            sha_value(data["merge_base_commit"]["sha"])
            for file in data["files"]:
                if not isinstance(file["filename"], str) or not isinstance(file["status"], str):
                    raise ValueError()
            return data
        except (ValueError, TypeError, KeyError):
            raise GitHubError("invalid_response") from None

    def commit(self, repo: str, sha: str) -> dict:
        data = self._json(self._get(f"/repos/{repo_name(repo)}/git/commits/{sha_value(sha)}"))
        try:
            if sha_value(data["sha"]) != sha_value(sha):
                raise ValueError()
            sha_value(data["tree"]["sha"])
            return data
        except (ValueError, TypeError, KeyError):
            raise GitHubError("invalid_response") from None

    def tree(self, repo: str, sha: str) -> list[dict]:
        data = self._json(self._get(f"/repos/{repo_name(repo)}/git/trees/{sha_value(sha)}", params={"recursive": "1"}))
        try:
            if data["truncated"] or len(data["tree"]) > self.settings.github_max_files:
                raise GitHubError("incomplete_result")
            if not isinstance(data["tree"], list):
                raise ValueError()
            for entry in data["tree"]:
                sha_value(entry["sha"])
                if not isinstance(entry["path"], str) or entry["type"] not in {"blob", "tree", "commit"}:
                    raise ValueError()
            return data["tree"]
        except (ValueError, KeyError, TypeError):
            raise GitHubError("invalid_response") from None

    def blob(self, repo: str, sha: str) -> bytes:
        data = self._json(self._get(f"/repos/{repo_name(repo)}/git/blobs/{sha_value(sha)}"))
        try:
            if data["size"] > self.settings.max_upload_bytes:
                raise GitHubError("response_too_large")
            if data["encoding"] != "base64":
                raise ValueError()
            content = base64.b64decode("".join(data["content"].split()), validate=True)
            if len(content) != data["size"] or len(content) > self.settings.max_upload_bytes:
                raise ValueError()
            return content
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
            raise GitHubError("invalid_response") from None

    def close(self):
        self._http.close()
