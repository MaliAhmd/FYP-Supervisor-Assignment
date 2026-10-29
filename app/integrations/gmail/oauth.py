"""Explicit desktop OAuth, separate purpose-specific credentials, private storage."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = {
    'read': ['https://www.googleapis.com/auth/gmail.readonly'],
    'send': ['https://www.googleapis.com/auth/gmail.send'],
}


class OAuthError(RuntimeError):
    pass


def protect(path):
    """Owner-only POSIX permissions or a fresh, non-inheriting Windows DACL."""
    path = Path(path)
    if path.is_symlink():
        raise OAuthError('token_path_must_not_be_symlink')
    if os.name != 'nt':
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    script = r'''
$ErrorActionPreference = 'Stop'
$target = Get-Item -LiteralPath $env:SUPERVISORAI_PRIVATE_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
if ($target.PSIsContainer) {
  $acl = New-Object System.Security.AccessControl.DirectorySecurity
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
} else {
  $acl = New-Object System.Security.AccessControl.FileSecurity
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
}
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $target.FullName -AclObject $acl
'''
    env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP'}}
    env['SUPERVISORAI_PRIVATE_PATH'] = str(path.resolve())
    try:
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], env=env,
                       capture_output=True, check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        raise OAuthError('could_not_protect_token_path') from None


def save_token(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    protect(path.parent)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.oauth-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            # Directory inheritance protects Windows files before any secret write.
            protect(Path(temporary))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        raise OAuthError('token_save_failed') from None
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def credentials(settings, purpose, *, authorize=False):
    if purpose not in SCOPES:
        raise ValueError('Unknown Gmail credential purpose')
    return scoped_credentials(settings, scopes=SCOPES[purpose], token_name=f'gmail-{purpose}.json', authorize=authorize)


def scoped_credentials(settings, *, scopes, token_name, authorize=False):
    """Shared private storage for explicitly scoped Google integrations."""
    if Path(token_name).name != token_name or not token_name.endswith('.json'):
        raise ValueError('Invalid credential filename')
    path = settings.token_dir / token_name
    try:
        creds = None
        if path.exists() and not authorize:
            protect(path.parent)
            protect(path)
            info = json.loads(path.read_text(encoding='utf-8'))
            if set(info.get('scopes', [])) != set(scopes):
                raise OAuthError('oauth_scope_mismatch')
            creds = Credentials.from_authorized_user_info(info)
        if authorize:
            if not settings.google_client_secrets_file:
                raise OAuthError('google_client_secrets_required')
            flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_client_secrets_file), scopes=scopes)
            creds = flow.run_local_server(host='127.0.0.1', port=0, access_type='offline', prompt='consent',
                                         authorization_prompt_message='Complete Google authorization in your browser.',
                                         success_message='Authorization completed. You may close this window.')
        if creds is None:
            raise OAuthError('google_authorization_required')
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid or set(creds.granted_scopes or creds.scopes or []) != set(scopes):
            raise OAuthError('google_reauthorization_required')
        save_token(path, creds.to_json())
        return creds
    except OAuthError:
        raise
    except Exception:
        raise OAuthError('google_authorization_failed') from None


def gmail_service(settings, purpose):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    creds = credentials(settings, purpose)
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=settings.request_timeout_seconds))
    return build('gmail', 'v1', http=http, cache_discovery=False, static_discovery=True)
