import pytest
from pydantic import ValidationError

from app.config import ROOT, Settings, validate_email


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_safe_defaults(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    config = settings()
    assert (config.llm_mode, config.github_mode, config.email_mode) == ("fake", "fake", "test")
    assert config.debounce_seconds == 300
    assert config.max_llm_calls_per_cycle == 6
    assert config.max_llm_calls_per_batch == 25
    assert config.max_coordinator_calls == 2
    assert not config.token_dir.is_relative_to(ROOT)


@pytest.mark.parametrize("values", [
    {"llm_mode": "live"}, {"github_mode": "live"}, {"email_mode": "invalid"},
    {"max_output_tokens": 801}, {"json_repair_retries": 2},
    {"max_llm_calls_per_cycle": 0}, {"token_dir": ROOT / "tokens"},
    {"smtp_port": 70000}, {"daily_token_quota": -1},
])
def test_invalid_configuration_rejected(values):
    with pytest.raises(ValidationError):
        settings(**values)


def test_live_models_are_configured_and_secrets_masked():
    config = settings(llm_mode="live", groq_api_key="private-key-123",
                      groq_model_fast="configured-fast", groq_model_smart="configured-smart")
    assert config.groq_model_fast == "configured-fast"
    assert "private-key-123" not in repr(config)
    assert "private-key-123" not in config.model_dump_json()


def test_env_example_loads():
    config = Settings(_env_file=ROOT / ".env.example")
    assert config.google_client_secrets_file is None
    assert config.database_path == ROOT / "data" / "supervisorai.sqlite3"
    for key in ('groq_api_key', 'github_token', 'github_client_secret', 'smtp_password'):
        assert not getattr(config, key).get_secret_value(), f'{key} must be empty in the shareable example'


@pytest.mark.parametrize("email", ["a@example.com\r\nBcc: b@example.com", "a@example.com,b@example.com", "Name <a@example.com>", "invalid"])
def test_email_validation_blocks_header_injection(email):
    with pytest.raises(ValueError):
        validate_email(email)
