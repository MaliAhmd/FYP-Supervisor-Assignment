"""Run explicitly to authorize read-only Gmail or optional Gmail delivery."""
import argparse
from app.config import Settings
from app.integrations.gmail.oauth import OAuthError, credentials


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('purpose', choices=('read', 'send'))
    args = parser.parse_args()
    try:
        credentials(Settings(), args.purpose, authorize=True)
    except OAuthError as error:
        parser.exit(1, str(error) + '\n')
    print('Gmail authorization saved in the configured private token directory.')


if __name__ == '__main__':
    main()
