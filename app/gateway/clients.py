"""One small Groq HTTP boundary plus a deterministic scripted fake."""

from collections import deque
from collections.abc import Mapping
import json

import httpx

from app.config import Settings
from app.gateway.types import CompletionRequest, CompletionResponse, ProviderError


class GroqClient:
    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        if not settings.groq_api_key.get_secret_value():
            raise ValueError("GROQ_API_KEY is required")
        self._reasoning_effort = settings.groq_reasoning_effort
        self._http = httpx.Client(
            headers={"Authorization": "Bearer " + settings.groq_api_key.get_secret_value()},
            timeout=settings.request_timeout_seconds,
            transport=transport, follow_redirects=False,
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        options = {"reasoning_effort": self._reasoning_effort} if self._reasoning_effort else {}
        try:
            response = self._http.post(self.ENDPOINT, json={
                "model": request.model, "messages": request.messages,
                "response_format": {"type": "json_object"},
                "max_completion_tokens": request.max_output_tokens,
                "stream": False,
                **options,
            })
        except httpx.HTTPError:
            raise ProviderError("transport_error", retryable=True) from None
        headers = {key.lower(): value for key, value in response.headers.items()
                   if key.lower().startswith("x-ratelimit-") or key.lower() == "retry-after"}
        if response.status_code != 200:
            code = {401: "provider_auth_error", 403: "provider_access_error",
                    400: "provider_request_error", 429: "rate_limited"}.get(response.status_code, "provider_http_error")
            # Only allowlisted categories leave this boundary. Never retain the
            # provider message or failed_generation (which may echo evidence).
            try:
                detail = response.json().get("error", {})
                provider_code = detail.get("code") if isinstance(detail, dict) else None
                if provider_code == "json_validate_failed":
                    code = "provider_json_error"
                elif provider_code in ("model_not_found", "model_decommissioned"):
                    code = "provider_model_error"
            except (ValueError, AttributeError):
                pass
            raise ProviderError(
                code,
                status=response.status_code, headers=headers,
                retryable=response.status_code in {408, 429, 500, 502, 503, 504},
            )
        try:
            body = response.json()
            choice = body["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or choice["message"].get("tool_calls"):
                raise ValueError()
            usage = body.get("usage") or {}
            tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
            for value in (tokens_in, tokens_out):
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError()
            return CompletionResponse(content, tokens_in, tokens_out, headers, choice.get("finish_reason", "stop"))
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("malformed_provider_response", headers=headers) from None

    def close(self) -> None:
        self._http.close()


class FakeLLMClient:
    """No schema guessing or evidence-driven actions. Responses are trusted fixtures.

    Script entries may be response objects, errors, JSON strings, or dictionaries.
    Later agents supply their own fixture mapping; the default supports the demo.
    """

    def __init__(self, script=(), *, responses: Mapping | None = None):
        self.script = deque(script)
        self.responses = dict(responses or {})
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        default = {"objectives": [], "milestones": [], "warnings": ["Fake mode: enter and confirm the baseline manually."]} if request.agent == "baseline" else {
            "status": "unverified", "summary": "Deterministic fake response; supervisor review required.", "evidence_ids": [],
        }
        value = self.script.popleft() if self.script else self.responses.get(request.agent, default)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, CompletionResponse):
            return value
        content = value if isinstance(value, str) else json.dumps(value)
        return CompletionResponse(content, request.estimated_input_tokens, min(len(content.encode()), request.max_output_tokens))

    def close(self) -> None:
        pass
