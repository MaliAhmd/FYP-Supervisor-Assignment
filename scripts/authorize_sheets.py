"""Explicit desktop authorization for Sheets, separate from Gmail credentials."""
from app.config import Settings
from app.integrations.gmail.oauth import OAuthError
from app.integrations.sheets.oauth import credentials


def main():
    try:
        credentials(Settings(), authorize=True)
    except OAuthError as error:
        raise SystemExit(str(error)) from None
    print('Sheets authorization saved in the configured private token directory.')


if __name__ == '__main__':
    main()
