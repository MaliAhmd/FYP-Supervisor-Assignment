"""Offline configuration inspection. Never print values, paths or exceptions."""
import json

from pydantic import ValidationError

from app.config import Settings


def inspect_settings(settings):
    """Report local prerequisites only; presence does not prove provider access."""
    warnings = []
    present = {}
    for name in ('groq_api_key', 'github_token', 'smtp_password'):
        present[name.upper()] = bool(getattr(settings, name).get_secret_value())
    for name in ('groq_model_fast', 'groq_model_smart', 'smtp_username', 'google_sheet_id'):
        present[name.upper()] = bool(getattr(settings, name).strip())
    if not present['GROQ_MODEL_FAST'] or not present['GROQ_MODEL_SMART']:
        warnings.append('Set both GROQ_MODEL_FAST and GROQ_MODEL_SMART before using live LLM mode.')
    if settings.test_redirect_email.lower().endswith('@example.com'):
        warnings.append('Replace TEST_REDIRECT_EMAIL with your own mailbox before sending test email.')
    if settings.smtp_from.lower().endswith('@example.com'):
        warnings.append('Replace SMTP_FROM with a sender mailbox authorized by your email provider.')
    if settings.email_sender == 'smtp' and (settings.smtp_host == 'localhost' or not present['SMTP_USERNAME'] or not present['SMTP_PASSWORD']):
        warnings.append('Check SMTP_HOST, SMTP_USERNAME and SMTP_PASSWORD before using authenticated SMTP.')
    google = {'client_file_exists': False, 'desktop_client_json': False}
    path = settings.google_client_secrets_file
    try:
        google['client_file_exists'] = bool(path and path.is_file())
        if google['client_file_exists']:
            # Bound the read; never report JSON contents, paths or parser errors.
            with path.open('rb') as handle:
                raw = handle.read(65537)
            document = json.loads(raw) if len(raw) <= 65536 else None
            installed = document.get('installed') if isinstance(document, dict) else None
            google['desktop_client_json'] = isinstance(installed, dict) and all(
                isinstance(installed.get(key), str) and bool(installed[key])
                for key in ('client_id', 'client_secret', 'auth_uri', 'token_uri'))
    except (OSError, ValueError):
        pass
    if not google['desktop_client_json']:
        warnings.append('Google features need GOOGLE_CLIENT_SECRETS_FILE pointing to a downloaded Desktop OAuth client JSON.')
    for filename in ('gmail-read.json', 'gmail-send.json', 'sheets.json'):
        try:
            google[filename + '_exists'] = (settings.token_dir / filename).is_file()
        except OSError:
            google[filename + '_exists'] = False
    if not google['gmail-read.json_exists']:
        warnings.append('Authorize Gmail read before enabling reply polling: python -m scripts.authorize_gmail read')
    if settings.email_sender == 'gmail' and not google['gmail-send.json_exists']:
        warnings.append('Authorize Gmail send before delivery: python -m scripts.authorize_gmail send')
    if not present['GOOGLE_SHEET_ID'] or not google['sheets.json_exists']:
        warnings.append('Sheets needs GOOGLE_SHEET_ID and authorization: python -m scripts.authorize_sheets')
    return {'settings_valid': True, 'modes': {
        name: getattr(settings, name) for name in ('llm_mode', 'github_mode', 'email_mode', 'email_sender')
    }, 'configured': present, 'google_local_files': google, 'warnings': warnings,
        'scope': 'Local checks only. No network calls, token refreshes or writes. Credentials and permissions are not verified.'}


def main():
    try:
        settings = Settings()
    except ValidationError as error:
        # Even Pydantic error contexts can contain inputs or custom exceptions.
        # Output only known field names and fixed guidance.
        fields = sorted({str(item['loc'][0]).upper() if item['loc'] and item['loc'][0] in Settings.model_fields
                         else 'SETTINGS' for item in error.errors(include_input=False, include_context=False)})
        print(json.dumps({'settings_valid': False, 'invalid_fields': fields,
            'help': 'Mailboxes must be bare addresses (name@example.com). Check field types and ranges. Live modes require credentials and live LLM mode requires both model names. TOKEN_DIR must be outside the source tree.'}, indent=2))
        return 1
    print(json.dumps(inspect_settings(settings), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
