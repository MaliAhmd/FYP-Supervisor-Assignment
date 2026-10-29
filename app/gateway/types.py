"""Contracts shared by the queue, transports, and future agents."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class GatewayError(RuntimeError):
    """Safe error code only: do not carry provider bodies or credentials."""


class BudgetExceeded(GatewayError):
    pass


class ProviderError(GatewayError):
    def __init__(self, code: str, *, status: int | None = None, headers: dict | None = None, retryable: bool = False):
        if code not in {"rate_limited", "provider_http_error", "transport_error", "malformed_provider_response",
                        "provider_auth_error", "provider_access_error", "provider_request_error",
                        "provider_json_error", "provider_model_error"}:
            code = "provider_error"
        super().__init__(code)
        self.status = status
        self.headers = headers or {}
        self.retryable = retryable


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    prompt: str
    output_schema: type[BaseModel]
    tier: Literal["fast", "smart"] = "fast"
    allowed_tools: tuple[str, ...] = ()

    def __post_init__(self):
        if self.name not in {"baseline", "coordinator", "repo_reviewer", "report_reviewer", "claims_comms"}:
            raise ValueError("Unknown agent")
        if self.tier not in {"fast", "smart"}:
            raise ValueError("Unknown model tier")
        safe_tools = {"load_memory", "read_diff", "read_evidence", "retrieve_passages", "check_feedback"}
        if not set(self.allowed_tools) <= safe_tools:
            raise ValueError("Tool is not in the read-only allowlist")
        if not issubclass(self.output_schema, BaseModel):
            raise ValueError("Output must use a Pydantic schema")


@dataclass(frozen=True)
class CompletionRequest:
    project_id: str
    cycle_id: str
    agent: str
    model: str
    messages: list[dict[str, str]]
    max_output_tokens: int
    estimated_input_tokens: int


@dataclass(frozen=True)
class CompletionResponse:
    content: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    finish_reason: str = "stop"


class LLMClient(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResponse: ...
    def close(self) -> None: ...


class JobResult(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "pending", "failed"]
    value: Any = None
    reason: str | None = None


class GatewayDemoOutput(StrictOutput):
    status: Literal["unverified"]
    summary: str = Field(max_length=300)
    evidence_ids: list[str]
