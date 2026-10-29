"""Separate Sheets credentials; Gmail scopes are never expanded."""
from app.integrations.gmail.oauth import scoped_credentials

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def credentials(settings, *, authorize=False):
    return scoped_credentials(settings, scopes=SCOPES, token_name='sheets.json', authorize=authorize)


def sheets_service(settings):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    http = AuthorizedHttp(credentials(settings), http=httplib2.Http(timeout=settings.request_timeout_seconds))
    return build('sheets', 'v4', http=http, cache_discovery=False, static_discovery=True)
