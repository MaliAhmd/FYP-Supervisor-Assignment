"""The only permitted GitHub mutation. Called solely by supervisor application code."""

import time
import httpx

from app.config import Settings
from app.integrations.github.client import API_ROOT, auth_headers, check_response
from app.integrations.github.models import GitHubError


def accept_repository_invitation(settings: Settings, invitation_id: int, *, transport=None, clock=time.time) -> None:
    if type(invitation_id) is not int or invitation_id <= 0:
        raise ValueError("Invalid invitation ID")
    # No retries on ambiguous writes. Refresh invitations/access before another decision.
    with httpx.Client(headers=auth_headers(settings), timeout=settings.request_timeout_seconds,
                      follow_redirects=False, transport=transport) as client:
        try:
            response = client.patch(f"{API_ROOT}/user/repository_invitations/{invitation_id}")
        except httpx.HTTPError:
            raise GitHubError("transport_error") from None
        check_response(response, clock(), allowed=(204, 304))
