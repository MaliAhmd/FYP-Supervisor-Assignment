"""Explicit register sync or local Excel export; never scheduled on import."""
import argparse
import json
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.integrations.sheets.client import SheetsClient, SheetsError
from app.integrations.sheets.excel import export_excel
from app.integrations.sheets.oauth import sheets_service
from app.orchestrator.sheets import RegisterSync


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--sync', action='store_true', help='Read and update the configured Google register')
    action.add_argument('--excel', type=Path, help='Create a new local .xlsx snapshot without Google access')
    args = parser.parse_args()
    settings = Settings()
    db = Database(settings.database_path)
    if not db.path.exists():
        parser.error('Initialize the database first')
    db.migrate()
    if args.excel:
        print('Export created:', export_excel(db, args.excel))
        return
    try:
        result = RegisterSync(db, settings, SheetsClient(sheets_service(settings), settings)).sync()
    except SheetsError as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, indent=2))
    if result['status'] != 'complete' or result['conflicts']:
        parser.exit(2, 'Register has pending work or conflicts; no conflicting row was overwritten.\n')


if __name__ == '__main__':
    main()
