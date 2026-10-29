import json

from app.config import Settings
from scripts import check_env


def config(**values):
    defaults = {name: field.get_default(call_default_factory=True) for name, field in Settings.model_fields.items()}
    defaults.update(values)
    return Settings(_env_file=None, **defaults)


def test_check_never_prints_credentials_or_file_contents(tmp_path):
    path = tmp_path / 'client.json'
    path.write_text(json.dumps({'installed': {key: 'private-marker' for key in
        ('client_id', 'client_secret', 'auth_uri', 'token_uri')}}))
    result = check_env.inspect_settings(config(groq_api_key='private-marker', github_token='private-marker',
        smtp_password='private-marker', smtp_username='private-marker', groq_model_fast='private-marker',
        google_client_secrets_file=path, token_dir=tmp_path))
    assert result['google_local_files']['desktop_client_json']
    assert 'private-marker' not in json.dumps(result)
    assert str(path) not in json.dumps(result)
    assert result['warnings']


def test_invalid_configuration_reports_field_without_input(monkeypatch, capsys):
    def invalid():
        return config(smtp_from='private-marker invalid')
    monkeypatch.setattr(check_env, 'Settings', invalid)
    # Preserve schema metadata used to whitelist error locations.
    invalid.model_fields = Settings.model_fields
    assert check_env.main() == 1
    output = capsys.readouterr().out
    assert 'SMTP_FROM' in output
    assert 'private-marker' not in output


def test_bad_google_json_is_safe_and_does_not_create_tokens(tmp_path):
    path = tmp_path / 'client.json'
    path.write_text('private-marker invalid json')
    tokens = tmp_path / 'tokens'
    result = check_env.inspect_settings(config(google_client_secrets_file=path, token_dir=tokens))
    assert not result['google_local_files']['desktop_client_json']
    assert not tokens.exists()
    assert 'private-marker' not in json.dumps(result)
