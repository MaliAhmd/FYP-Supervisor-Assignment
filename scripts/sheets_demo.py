"""Isolated register conflict/restart demo and optional local Excel artifact."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.db import Database, ProjectStore
from app.integrations.sheets.excel import export_excel
from app.integrations.sheets.fake import FakeSheetsClient
from app.orchestrator.sheets import RegisterSync


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--excel', type=Path, help='Create a new .xlsx export of the fictional demo data')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='supervisorai-register-') as directory:
        db = Database(Path(directory) / 'demo.sqlite3')
        db.migrate()
        settings = Settings(_env_file=None, llm_mode='fake', github_mode='fake', email_mode='test', google_sheet_id='fake-register')
        first = ProjectStore.create(db, 'Fictional Amina', 'amina@example.com', 'fictional/amina')
        second = ProjectStore.create(db, 'Fictional Bilal', 'bilal@example.com', 'fictional/bilal')
        fake = FakeSheetsClient([[''] * 12 + ['Supervisor notes']])
        sync = RegisterSync(db, settings, fake)
        assert len(sync.sync()['updated']) == 2 and len(fake.writes) == 1
        first_row = next(r for r in fake.rows[1:] if r[0] == first.project_id)
        first_row.append('Keep this manual note.')
        first_row[1] = 'Manual generated-cell edit'
        with db.transaction() as conn:
            conn.execute("UPDATE projects SET status='paused' WHERE id=?", (second.project_id,))
        fake.fail_after_write = True
        assert sync.sync()['status'] == 'pending'
        fake.fail_after_write = False
        restored = RegisterSync(Database(db.path), settings, fake).sync()
        assert first.project_id in restored['conflicts']
        assert second.project_id in restored['unchanged']
        assert first_row[12] == 'Keep this manual note.'
        assert len(fake.writes) == 2
        if args.excel:
            export_excel(db, args.excel)
            print('Created fictional register export:', args.excel)
        print('One initial batch; manual notes preserved; generated-cell edit reported.')
        print('Recovered accepted write after restart without replaying it. No live Google API used.')


if __name__ == '__main__':
    main()
