"""Failure boundaries and recovery across the remote/local commit gap."""
from datetime import date
import sqlite3

import pytest

from app.config import Settings
from app.db import ProjectStore
from app.integrations.sheets.fake import FakeSheetsClient
from app.orchestrator.sheets import RegisterSync


def settings():
    return Settings(_env_file=None, llm_mode='fake', github_mode='fake', email_mode='test', google_sheet_id='fake')


def test_remote_success_local_commit_failure_recovers(db, project):
    fake = FakeSheetsClient()
    sync = RegisterSync(db, settings(), fake)
    with db.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_finalize BEFORE UPDATE ON register_sync WHEN NEW.pending_hash IS NULL BEGIN SELECT RAISE(ABORT,'test'); END;")
    with pytest.raises(sqlite3.IntegrityError): sync.sync()
    assert len(fake.writes) == 1
    with db.transaction() as conn:
        conn.execute('DROP TRIGGER fail_finalize')
    assert sync.sync()['unchanged'] == [project.project_id]
    assert len(fake.writes) == 1


def test_database_change_after_lost_response_writes_latest_values(db, project):
    fake = FakeSheetsClient()
    fake.fail_after_write = True
    sync = RegisterSync(db, settings(), fake)
    assert sync.sync()['status'] == 'pending'
    fake.fail_after_write = False
    with db.transaction() as conn:
        conn.execute("UPDATE projects SET status='paused' WHERE id=?", (project.project_id,))
    assert sync.sync()['updated'] == [project.project_id]
    assert fake.rows[1][4] == 'paused' and len(fake.writes) == 2


def test_new_project_during_unaccepted_pending_batch_gets_distinct_row(db, project):
    fake = FakeSheetsClient()
    fake.fail_before_write = True
    sync = RegisterSync(db, settings(), fake)
    sync.sync()
    other = ProjectStore.create(db, 'Later', 'later@example.com', 'later/repo')
    fake.fail_before_write = False
    result = sync.sync()
    assert not result['conflicts']
    assert {r[0] for r in fake.rows[1:]} == {project.project_id, other.project_id}


def test_manual_data_in_reserved_blank_row_blocks_retry(db, project):
    fake = FakeSheetsClient()
    fake.fail_before_write = True
    sync = RegisterSync(db, settings(), fake)
    sync.sync()
    fake.fail_before_write = False
    fake.rows = [[], [''] * 12 + ['Inserted while offline']]
    result = sync.sync()
    assert result['conflicts'][project.project_id] == 'project_id_missing'
    assert fake.rows[1][12] == 'Inserted while offline'


def test_scopes_and_token_file_are_separate(monkeypatch):
    from app.integrations.sheets import oauth
    captured = {}
    def load(settings, **kwargs):
        captured.update(kwargs)
        return 'credential'
    monkeypatch.setattr(oauth, 'scoped_credentials', load)
    assert oauth.credentials(settings(), authorize=True) == 'credential'
    assert captured['token_name'] == 'sheets.json'
    assert captured['scopes'] == ['https://www.googleapis.com/auth/spreadsheets']
    assert captured['authorize'] is True
