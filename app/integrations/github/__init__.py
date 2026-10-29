"""Read-only GitHub adapter and supervisor-only invitation acceptance."""

from app.integrations.github.client import GitHubClient
from app.integrations.github.fake import FakeGitHubClient


def create_github_reader(settings, **kwargs):
    return FakeGitHubClient() if settings.github_mode == "fake" else GitHubClient(settings, **kwargs)


__all__ = ["GitHubClient", "FakeGitHubClient", "create_github_reader"]
