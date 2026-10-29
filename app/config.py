"""Validated settings. Importing this module performs no network or file writes."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
        case_sensitive=False, hide_input_in_errors=True,
    )

    llm_mode: Literal["fake", "live"] = "fake"
    github_mode: Literal["fake", "live"] = "fake"
    email_mode: Literal["test", "live"] = "test"
    email_sender: Literal["smtp", "gmail"] = "smtp"
    test_redirect_email: str = "supervisor-test@example.com"
    database_path: Path = ROOT / "data" / "supervisorai.sqlite3"
    token_dir: Path = Path.home() / ".supervisorai" / "tokens"
    groq_api_key: SecretStr = SecretStr("")
    groq_model_fast: str = ""
    groq_model_smart: str = ""
    groq_reasoning_effort: Literal["", "low", "medium", "high"] = ""
    github_token: SecretStr = SecretStr("")
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    github_api_version: str = "2026-03-10"
    github_max_pages: int = Field(default=10, ge=1, le=100)
    github_max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    github_max_files: int = Field(default=3000, ge=1)
    github_backoff_max_seconds: int = Field(default=3600, ge=60)
    google_client_secrets_file: Path | None = None
    google_sheet_id: str = ""
    google_sheet_tab: str = "Progress"
    sheets_max_rows: int = Field(default=5000, ge=2, le=100000)
    sheets_max_columns: int = Field(default=256, ge=12, le=18278)
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = "supervisor@example.com"
    smtp_tls_mode: Literal["starttls", "ssl"] = "starttls"
    poll_seconds: int = Field(default=60, ge=1)
    reply_poll_seconds: int = Field(default=300, ge=1)
    gmail_pages_per_tick: int = Field(default=3, ge=1, le=100)
    email_max_bytes: int = Field(default=1024 * 1024, ge=1024)
    email_max_body_chars: int = Field(default=100000, ge=100)
    debounce_seconds: int = Field(default=300, ge=0)
    max_llm_calls_per_cycle: int = Field(default=6, ge=1)
    max_llm_calls_per_batch: int = Field(default=25, ge=1)
    max_coordinator_calls: int = Field(default=2, ge=1, le=2)
    max_specialist_tool_calls: int = Field(default=6, ge=1)
    max_input_tokens: int = Field(default=3000, ge=128)
    max_output_tokens: int = Field(default=700, ge=1, le=800)
    json_repair_retries: int = Field(default=1, ge=0, le=1)
    daily_token_quota: int = Field(default=30000, ge=1)
    max_http_retries: int = Field(default=3, ge=0, le=10)
    request_timeout_seconds: float = Field(default=30, gt=0)
    report_top_k: int = Field(default=4, ge=1)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    pdf_max_pages: int = Field(default=200, ge=1, le=2000)
    pdf_max_text_chars: int = Field(default=200000, ge=100)
    pdf_max_stream_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    pdf_timeout_seconds: float = Field(default=30, gt=0, le=300)
    pdf_passage_chars: int = Field(default=600, ge=100, le=4000)
    max_diff_chars_per_file: int = Field(default=8000, ge=1)
    # Local pacing defaults, not claims about a provider's account limits.
    llm_requests_per_minute: int = Field(default=10, ge=1)
    llm_tokens_per_minute: int = Field(default=6000, ge=1)
    llm_backoff_base_seconds: float = Field(default=1, gt=0)
    llm_backoff_max_seconds: float = Field(default=60, gt=0)

    @field_validator("google_client_secrets_file", mode="before")
    @classmethod
    def empty_optional_path(cls, value):
        return None if value == "" else value

    @field_validator("database_path", "token_dir", "google_client_secrets_file")
    @classmethod
    def resolve_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        expanded = value.expanduser()
        return (ROOT / expanded).resolve() if not expanded.is_absolute() else expanded.resolve()

    @field_validator("smtp_from", "test_redirect_email")
    @classmethod
    def validate_mailbox(cls, value: str) -> str:
        return validate_email(value)

    @model_validator(mode="after")
    def check_live_credentials(self):
        if self.llm_mode == "live" and not (
            self.groq_api_key.get_secret_value() and self.groq_model_fast.strip()
            and self.groq_model_smart.strip()
        ):
            raise ValueError("Live LLM mode requires GROQ_API_KEY and both GROQ_MODEL settings")
        if self.github_mode == "live" and not self.github_token.get_secret_value():
            raise ValueError("Live GitHub mode requires GITHUB_TOKEN")
        if self.token_dir.is_relative_to(ROOT):
            raise ValueError("TOKEN_DIR must be outside the source tree")
        return self


def validate_email(value: str) -> str:
    """Accept a single bare mailbox; reject header injection and address lists."""
    import re

    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value):
        raise ValueError("Expected a single email address without display name or headers")
    return value
