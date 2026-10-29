from datetime import date
from pathlib import Path
import shutil

import pytest
from openpyxl import load_workbook

from app.config import ROOT, Settings
from app.db import Database, ProjectStore
from app.gateway.prompts import digest
from app.gateway.types import GatewayError
from app.integrations.sheets.client import SheetsClient, SheetsError, column_name, quoted_tab
from app.integrations.sheets.excel import export_excel
from app.integrations.sheets.fake import FakeSheetsClient
from app.integrations.sheets.register import HEADERS, register_rows
from app.orchestrator.sheets import RegisterSync, generated


@pytest.fixture
def config():
    return Settings(_env_file=None, llm_mode='fake', github_mode='fake', email_mode='test', google_sheet_id='test-sheet')


def test_batch_once_and_preserve_manual_columns(db, project, config):
    other = ProjectStore.create(db, 'Other', 'other@example.com', 'other/repo')
    fake = FakeSheetsClient([[''] * 12 + ['Notes', 'Decision']])
    sync = RegisterSync(db, config, fake)
    result = sync.sync()
    assert set(result['updated']) == {project.project_id, other.project_id}
    assert len(fake.writes) == 1 and len(fake.writes[0]) == 3
    fake.rows[1].extend(['Manual note', '=1+2'])
    with db.transaction() as conn:
        conn.execute("UPDATE projects SET status='paused' WHERE id=?", (project.project_id,))
    sync.sync()
    assert fake.rows[0][12:] == ['Notes', 'Decision']
    assert fake.rows[1][12:] == ['Manual note', '=1+2']
    assert all(len(values) == 12 for batch in fake.writes for _, values in batch)
    assert sync.sync()['updated'] == []
    assert len(fake.writes) == 2


def test_generated_edit_conflicts_but_other_project_updates(db, project, config):
    other = ProjectStore.create(db, 'Other', 'other@example.com', 'other/repo')
    fake = FakeSheetsClient()
    sync = RegisterSync(db, config, fake)
    sync.sync()
    edited = next(row for row in fake.rows if row[0] == project.project_id)
    edited[1] = 'Human override'
    with db.transaction() as conn:
        conn.execute("UPDATE projects SET status='paused' WHERE id=?", (other.project_id,))
    result = sync.sync()
    assert result['conflicts'] == {project.project_id: 'generated_cells_edited'}
    assert result['updated'] == [other.project_id]
    assert edited[1] == 'Human override'


@pytest.mark.parametrize('edit', ['formula', 'numeric', 'blank'])
def test_formula_numeric_and_cleared_cells_are_edits(db, project, config, edit):
    fake = FakeSheetsClient()
    sync = RegisterSync(db, config, fake)
    sync.sync()
    fake.rows[1][7] = {'formula': '=0', 'numeric': 0, 'blank': ''}[edit]
    assert project.project_id in sync.sync()['conflicts']
    assert len(fake.writes) == 1


@pytest.mark.parametrize('accepted', [True, False])
def test_uncertain_batch_restart_recovery(db, project, config, accepted):
    fake = FakeSheetsClient()
    if accepted: fake.fail_after_write = True
    else: fake.fail_before_write = True
    result = RegisterSync(db, config, fake).sync()
    assert result['status'] == 'pending' and result['updated'] == []
    with db.transaction(write=False) as conn:
        row = conn.execute('SELECT * FROM register_sync WHERE project_id=?', (project.project_id,)).fetchone()
        assert row['pending_hash'] and row['generated_hash'] is None
    fake.fail_after_write = fake.fail_before_write = False
    result = RegisterSync(Database(db.path), config, fake).sync()
    assert result['status'] == 'complete'
    assert len(fake.writes) == 1
    assert len([r for r in fake.rows if r and r[0] == project.project_id]) == 1


def test_edit_after_uncertain_success_not_overwritten(db, project, config):
    fake = FakeSheetsClient()
    fake.fail_after_write = True
    RegisterSync(db, config, fake).sync()
    fake.rows[1][1] = 'New human text'
    fake.fail_after_write = False
    assert RegisterSync(db, config, fake).sync()['conflicts'][project.project_id] == 'generated_cells_edited'
    assert len(fake.writes) == 1


def test_rows_sorted_and_manual_only_rows_not_reused(db, project, config):
    other = ProjectStore.create(db, 'Other', 'other@example.com', 'other/repo')
    fake = FakeSheetsClient([HEADERS + ['Notes'], [''] * 12 + ['Existing manual-only row']])
    sync = RegisterSync(db, config, fake)
    sync.sync()
    assert fake.rows[1][0] == ''
    fake.rows[2], fake.rows[3] = fake.rows[3], fake.rows[2]
    result = sync.sync()
    assert not result['conflicts'] and not result['updated']
    with db.transaction(write=False) as conn:
        mappings = {r['project_id']: r['sheet_row'] for r in conn.execute('SELECT * FROM register_sync')}
    assert fake.rows[mappings[project.project_id]-1][0] == project.project_id
    assert fake.rows[mappings[other.project_id]-1][0] == other.project_id


@pytest.mark.parametrize('change,reason', [('duplicate', 'duplicate_project_id'), ('delete_id', 'project_id_missing')])
def test_identity_conflicts(db, project, config, change, reason):
    fake = FakeSheetsClient()
    sync = RegisterSync(db, config, fake)
    sync.sync()
    if change == 'duplicate': fake.rows.append(list(fake.rows[1]))
    else: fake.rows[1][0] = ''
    assert sync.sync()['conflicts'] == {project.project_id: reason}
    assert len(fake.writes) == 1


def test_wrong_headers_and_destination_refuse_writes(db, project, config):
    fake = FakeSheetsClient([['Unrelated workbook']])
    with pytest.raises(SheetsError, match='headers'):
        RegisterSync(db, config, fake).sync()
    assert not fake.writes
    fake.rows = []
    RegisterSync(db, config, fake).sync()
    with pytest.raises(SheetsError, match='destination'):
        RegisterSync(db, config.model_copy(update={'google_sheet_id': 'different'}), fake).sync()
    assert len(fake.writes) == 1


def test_acknowledgement_requires_exact_observed_hash(db, project, config):
    fake = FakeSheetsClient()
    sync = RegisterSync(db, config, fake)
    sync.sync()
    fake.rows[1][1] = 'Human edit'
    fake.rows[1].append('Keep note')
    observed = digest(generated(fake.rows[1]))
    with pytest.raises(ValueError):
        sync.acknowledge_generated_edit(project.project_id, observed_hash='stale')
    sync.acknowledge_generated_edit(project.project_id, observed_hash=observed)
    assert len(fake.writes) == 1
    assert sync.sync()['updated'] == [project.project_id]
    assert fake.rows[1][1] == project.project()['student_name'] and fake.rows[1][12] == 'Keep note'


def test_project_isolation_acknowledgement(db, project, config):
    fake = FakeSheetsClient()
    sync = RegisterSync(db, config, fake)
    sync.sync()
    other = ProjectStore.create(db, 'Other', 'other@example.com', 'other/repo')
    with pytest.raises(ValueError):
        sync.acknowledge_generated_edit(other.project_id, observed_hash=digest(generated(fake.rows[1])))


def test_register_approved_dates_delay_and_completion(db, project):
    pid, cid = project.project_id, project.create_cycle('review')
    evidence = project.add_evidence(cid, 'file_diff', 'code', 'done')
    with db.transaction() as conn:
        conn.execute("INSERT INTO milestones(id,project_id,title,completion_criteria) VALUES ('m',?,'Prototype','evaluation')", (pid,))
        conn.execute("INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,source,decided_by) VALUES ('d',?,'m','2026-01-01','approved','baseline','supervisor')", (pid,))
        conn.execute("INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,source) VALUES ('proposed',?,'m','2028-01-01','proposed','email')", (pid,))
    row = register_rows(db, today=date(2026, 10, 26))[pid]
    assert row[5:8] == ['0/1', '2026-01-01', '1']
    from app.orchestrator.deadlines import DeadlineService
    DeadlineService(db).confirm_completion(pid, 'm', evidence)
    assert register_rows(db, today=date(2026, 10, 26))[pid][5:8] == ['1/1', '', '0']


def test_capacity_is_explicit(db, project, config):
    fake = FakeSheetsClient([HEADERS, ['unknown'] + [''] * 11], row_capacity=2)
    assert RegisterSync(db, config, fake).sync()['conflicts'] == {project.project_id: 'sheet_row_capacity'}
    assert not fake.writes


def test_lock_prevents_overlapping_sync(db, project, config):
    sync = RegisterSync(db, config, FakeSheetsClient())
    lock = sync._lock()
    try:
        with pytest.raises(GatewayError): sync.sync()
    finally:
        lock.close()


class API:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kw): self.calls.append(('get', kw)); return self
    def batchGet(self, **kw): self.calls.append(('read', kw)); return self
    def batchUpdate(self, **kw): self.calls.append(('write', kw)); return self
    def execute(self, **kw):
        assert kw == {'num_retries': 0}
        result = self.responses.pop(0)
        if isinstance(result, Exception): raise result
        return result


def test_adapter_quotes_ranges_reads_formulas_and_writes_only_raw_generated(config):
    tab = "Supervisor's register"
    api = API([{'sheets': [{'properties': {'title': tab, 'gridProperties': {'rowCount': 1000, 'columnCount': 27}}}]},
               {'valueRanges': [{'values': [HEADERS]}]}, {'totalUpdatedCells': 12}])
    client = SheetsClient(api, config)
    assert client.read('id', tab).rows == [HEADERS]
    client.batch_write('id', tab, [(2, ['=literal'] * 12)])
    assert api.calls[1][1]['ranges'] == ["'Supervisor''s register'!A:AA"]
    assert api.calls[1][1]['valueRenderOption'] == 'FORMULA'
    body = api.calls[2][1]['body']
    assert body['valueInputOption'] == 'RAW'
    assert body['data'][0]['range'] == "'Supervisor''s register'!A2:L2"
    with pytest.raises(ValueError): client.batch_write('id', tab, [(2, ['bad'] * 13)])


def test_adapter_errors_are_safe_and_write_is_uncertain(config):
    with pytest.raises(SheetsError, match='read_failed') as caught:
        SheetsClient(API([RuntimeError('secret')]), config).read('id', 'Progress')
    assert 'secret' not in str(caught.value)
    with pytest.raises(SheetsError, match='uncertain'):
        SheetsClient(API([{}]), config).batch_write('id', 'Progress', [(1, HEADERS)])


def test_excel_literal_formulas_and_no_overwrite(db, project, tmp_path):
    with db.transaction() as conn:
        conn.execute("UPDATE projects SET student_name='=HYPERLINK(\"bad\")' WHERE id=?", (project.project_id,))
    path = export_excel(db, tmp_path / 'export.xlsx')
    workbook = load_workbook(path)
    sheet = workbook.active
    assert sheet['B2'].value.startswith('=') and sheet['B2'].data_type == 's'
    assert sheet['M1'].value == 'Supervisor notes' and sheet.freeze_panes == 'A2'
    workbook.close()
    original = path.read_bytes()
    with pytest.raises(FileExistsError): export_excel(db, path)
    assert path.read_bytes() == original


def test_migration_retains_legacy_register(tmp_path):
    migrations = tmp_path / 'old'
    migrations.mkdir()
    for source in sorted((ROOT / 'migrations').glob('00[1-6]*.sql')):
        shutil.copyfile(source, migrations / source.name)
    db = Database(tmp_path / 'legacy.sqlite3')
    db.migrate(migrations)
    project = ProjectStore.create(db, 'Legacy', 'legacy@example.com', 'legacy/repo')
    with db.transaction() as conn:
        conn.execute("INSERT INTO register_sync(project_id,sheet_row,generated_hash) VALUES (?,8,'oldhash')", (project.project_id,))
    db.migrate()
    with db.transaction(write=False) as conn:
        row = conn.execute('SELECT * FROM register_sync WHERE project_id=?', (project.project_id,)).fetchone()
        assert row['sheet_row'] == 8 and row['generated_hash'] == 'oldhash' and row['spreadsheet_id'] == ''


def test_empty_database_does_not_contact_google(db, config):
    fake = FakeSheetsClient()
    assert RegisterSync(db, config, fake).sync()['status'] == 'complete'
    assert not fake.reads and not fake.writes
