from datetime import date
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.config import ROOT, Settings
from app.db import ProjectStore
from app.ui.runtime import Runtime
from scripts.pdf_fixtures import text_pdf


@pytest.fixture
def config(tmp_path):
    return Settings(_env_file=None, database_path=tmp_path / 'ui.sqlite3', llm_mode='fake', github_mode='fake', email_mode='test',
                    llm_requests_per_minute=1000, llm_tokens_per_minute=100000)


@pytest.fixture
def runtime(config):
    rt = Runtime(config, start=False)
    yield rt
    rt.close()


def project(rt):
    return ProjectStore.create(rt.db, 'Amina Example', 'amina@example.com', 'fictional/amina')


def app(monkeypatch, rt):
    import app.ui.main as ui
    monkeypatch.setattr(ui, 'Settings', lambda: rt.settings)
    monkeypatch.setattr(ui, 'get_runtime', lambda settings: rt)
    return AppTest.from_file(str(ROOT / 'streamlit_app.py'), default_timeout=15).run()


def button(at, label):
    return next(b for b in at.button if b.label == label)


def test_empty_dashboard_and_project_creation(runtime, monkeypatch):
    at = app(monkeypatch, runtime)
    assert not at.exception
    assert any('Start in Projects' in item.value for item in at.info)
    at.sidebar.radio[0].set_value('Projects').run()
    at.text_input[0].set_value('Student One')
    at.text_input[1].set_value('one@example.com')
    at.text_input[2].set_value('fictional/one')
    button(at, 'Create project').click().run()
    assert not at.exception
    assert ProjectStore.list_projects(runtime.db)[0]['student_email'] == 'one@example.com'


@pytest.mark.parametrize('page', ['Dashboard', 'Setup', 'Projects', 'Review', 'Approvals', 'History', 'Register', 'Audit'])
def test_all_pages_render_project_without_live_io(runtime, monkeypatch, page):
    project(runtime)
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.sidebar.selectbox[0].value
    assert runtime.github.requests == []


def test_invalid_configuration_shows_safe_recovery(monkeypatch):
    import app.ui.main as ui
    def invalid():
        return Settings(_env_file=None, llm_mode='fake', github_mode='fake', smtp_from='secret-invalid-value')
    monkeypatch.setattr(ui, 'Settings', invalid)
    at = AppTest.from_file(str(ROOT / 'streamlit_app.py')).run()
    assert not at.exception
    assert any('Configuration needs attention' in item.value for item in at.error)
    assert all('secret-invalid-value' not in item.value for item in at.text)
    assert any('smtp_from' in item.value for item in at.text)


def test_review_button_queues_only(runtime, monkeypatch):
    p = project(runtime)
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Review').run()
    button(at, 'Review now').click().run()
    assert not at.exception and not runtime.github.requests
    with runtime.db.transaction(write=False) as conn:
        row = conn.execute('SELECT * FROM ui_tasks WHERE project_id=?', (p.project_id,)).fetchone()
    assert row['kind'] == 'review' and row['status'] == 'queued'


def test_plain_verification_result_is_text_not_json(runtime, monkeypatch):
    p = project(runtime)
    runtime.submit(p.project_id, 'verify')
    with runtime.db.transaction() as conn:
        conn.execute("UPDATE ui_tasks SET status='completed',result_json=? WHERE project_id=?", (json.dumps('active'), p.project_id))
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Projects').run()
    assert not at.exception
    assert any(x.value == 'active' for x in at.text)
    assert all(x.value != 'active' for x in at.json)
    assert any('not the current review' in x.value for x in at.caption)


def test_pending_proposal_has_explicit_retry_without_automatic_resend(runtime, monkeypatch):
    from app.gateway.clients import FakeLLMClient
    from app.gateway.types import ProviderError
    p = project(runtime)
    runtime.gateway.client.close()
    client = FakeLLMClient([ProviderError('provider_http_error', status=400)])
    runtime.gateway.client = client
    draft = runtime.baselines.upload(p.project_id, text_pdf(['Objective: catalogue books.']))
    runtime.baselines.request_extraction(p.project_id, draft['id'], 'batch')
    runtime.gateway.run_once()
    runtime.baselines.collect(p.project_id, draft['id'])
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Projects').run()
    assert not at.exception
    assert any('Groq rejected' in w.value for w in at.warning)
    assert len(client.requests) == 1
    button(at, 'Retry proposal extraction').click().run()
    assert not at.exception
    assert len(client.requests) == 1
    with runtime.db.transaction(write=False) as conn:
        task = conn.execute("SELECT kind,status FROM ui_tasks WHERE project_id=?", (p.project_id,)).fetchone()
    assert tuple(task) == ('extract', 'queued')


def test_review_batch_progress_and_explicit_continuation(runtime, monkeypatch):
    p = project(runtime)
    cid = p.create_cycle('a' * 40)
    state = {'repo_batches': [['first'], ['second']], 'repo_batch_index': 1, 'review_cycles': [cid]}
    with runtime.db.transaction() as conn:
        conn.execute("INSERT INTO review_state(project_id,cycle_id,batch_id,payload,reason) VALUES (?,?,?,?,'review_batch_complete')",
                     (p.project_id, cid, 'batch', json.dumps(state)))
        conn.execute("UPDATE cycles SET status='pending_evidence_review',stage='repo_reviewer' WHERE id=?", (cid,))
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Review').run()
    assert not at.exception
    assert any('Repository batches reviewed: 1 / 2' in x.value for x in at.markdown)
    button(at, 'Continue next review batch').click().run()
    assert not at.exception
    with runtime.db.transaction(write=False) as conn:
        task = conn.execute('SELECT kind,payload FROM ui_tasks WHERE project_id=?', (p.project_id,)).fetchone()
    assert task['kind'] == 'resume'
    assert json.loads(task['payload'])['id'] == cid


def test_background_upload_baseline_and_fake_review_restart(config):
    rt = Runtime(config, start=False)
    p = project(rt)
    pid = p.project_id
    rt.submit(pid, 'proposal', {'filename': 'proposal.pdf'}, text_pdf(['Objectives: evaluation prototype.']))
    rt.tick()
    draft = rt.baselines.drafts(pid)[0]
    edited = rt.baselines.edit(pid, draft['id'], {'objectives': ['Evaluation'], 'milestones': [{'title': 'Prototype', 'completion_criteria': 'evaluation', 'deadline': '2026-12-01', 'source_pages': [1]}], 'warnings': []}, expected_revision=draft['revision'])
    rt.baselines.confirm(pid, draft['id'], expected_revision=edited['revision'], acknowledge_limited_coverage=True)
    rt.submit(pid, 'fake_push')
    rt.tick()
    rt.submit(pid, 'verify')
    rt.tick()
    rt.submit(pid, 'review', {'batch_id': 'ui-test'})
    rt.tick()
    rt.gateway.run_once()
    rt.close()
    restarted = Runtime(config, start=False)
    try:
        for _ in range(25):
            restarted.tick()
            restarted.gateway.run_once()
        with restarted.db.transaction(write=False) as conn:
            cycle = conn.execute("SELECT * FROM cycles WHERE project_id=? AND trigger='manual'", (pid,)).fetchone()
            assert cycle['status'] == 'completed'
            assert conn.execute('SELECT count(*) FROM email_drafts WHERE project_id=?', (pid,)).fetchone()[0] == 1
            assert conn.execute('SELECT monitoring FROM ui_controls WHERE project_id=?', (pid,)).fetchone()[0] == 1
        assert ProjectStore(restarted.db, pid).project()['last_reviewed_sha'] == cycle['after_sha']
    finally:
        restarted.close()


def test_queued_actions_survive_restart_running_actions_not_replayed(config):
    rt = Runtime(config, start=False)
    pid = project(rt).project_id
    queued = rt.submit(pid, 'fake_push')
    interrupted = rt.submit(pid, 'send', {'id': 'draft'})
    with rt.db.transaction() as conn:
        conn.execute("UPDATE ui_tasks SET status='running' WHERE project_id=? AND id=?", (pid, interrupted))
    rt.close()
    fresh = Runtime(config, start=False)
    try:
        fresh.tick()
        with fresh.db.transaction(write=False) as conn:
            states = {r['id']: r['status'] for r in conn.execute('SELECT * FROM ui_tasks WHERE project_id=?', (pid,))}
        assert states == {queued: 'completed', interrupted: 'interrupted'}
    finally:
        fresh.close()


def test_task_dedupe_isolation_and_errors_are_safe(runtime, monkeypatch):
    pid = project(runtime).project_id
    key = runtime.submit(pid, 'fake_push')
    assert key == runtime.submit(pid, 'fake_push')
    with pytest.raises(LookupError): runtime.submit('other-project', 'fake_push')
    with pytest.raises(ValueError): runtime.submit(pid, 'arbitrary_function')
    def fail(task): raise RuntimeError('API_SECRET_PRIVATE')
    monkeypatch.setattr(runtime, '_action', fail)
    runtime.tick()
    with runtime.db.transaction(write=False) as conn:
        row = conn.execute('SELECT * FROM ui_tasks WHERE project_id=? AND id=?', (pid, key)).fetchone()
    assert row['status'] == 'failed' and 'PRIVATE' not in row['error']


def test_monitor_settings_and_project_edit_invalidate_mail_approval(runtime):
    p = project(runtime)
    pid, cid = p.project_id, p.create_cycle('fixture')
    with runtime.db.transaction() as conn:
        conn.execute("INSERT INTO email_drafts(id,project_id,cycle_id,to_addr,subject,body,message_id,idempotency_key,status,approved_at,approval_hash) VALUES ('draft',?,?,'amina@example.com','Review','Body','<test@example.com>','key','approved','2026-01-01','hash')", (pid, cid))
    runtime.monitoring(pid, True, 'batch', replies=True)
    runtime.edit_project(pid, 'Amina', 'new@example.com', paused=True)
    with runtime.db.transaction(write=False) as conn:
        row = conn.execute('SELECT * FROM email_drafts WHERE project_id=?', (pid,)).fetchone()
        assert row['status'] == 'pending' and row['approval_hash'] is None and row['to_addr'] == 'new@example.com'
        assert conn.execute('SELECT replies_enabled FROM ui_controls WHERE project_id=?', (pid,)).fetchone()[0] == 1
    assert p.project()['status'] == 'paused'


def test_approval_form_and_send_button_separation(runtime, monkeypatch):
    p = project(runtime)
    pid, cid = p.project_id, p.create_cycle('fixture')
    with runtime.db.transaction() as conn:
        conn.execute("INSERT INTO email_drafts(id,project_id,cycle_id,to_addr,subject,body,message_id,idempotency_key) VALUES ('draft',?,?,'amina@example.com','Review','Body','<test@example.com>','key')", (pid, cid))
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Approvals').run()
    assert not at.exception
    next(t for t in at.text_area if t.label == 'Email body').set_value('Edited by supervisor')
    button(at, 'Approve this text').click().run()
    assert not at.exception
    from app.orchestrator.email import EmailService
    saved = EmailService(runtime.db, runtime.settings, None).get(pid, 'draft')
    assert saved['status'] == 'approved' and saved['body'] == 'Edited by supervisor'
    with runtime.db.transaction(write=False) as conn:
        assert conn.execute('SELECT count(*) FROM email_attempts').fetchone()[0] == 0
    at.run()
    button(at, 'Send approved email').click().run()
    with runtime.db.transaction(write=False) as conn:
        assert conn.execute("SELECT count(*) FROM ui_tasks WHERE project_id=? AND kind='send' AND status='queued'", (pid,)).fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM email_attempts').fetchone()[0] == 0


def test_export_is_background_and_download_persisted(runtime, monkeypatch):
    pid = project(runtime).project_id
    key = runtime.submit(pid, 'export')
    runtime.tick()
    with runtime.db.transaction(write=False) as conn:
        row = conn.execute('SELECT * FROM ui_tasks WHERE project_id=? AND id=?', (pid, key)).fetchone()
    assert row['status'] == 'completed' and row['output_blob'].startswith(b'PK')
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Register').run()
    assert not at.exception
    assert len(at.get('download_button')) == 1


def test_configuration_changes_require_restart(runtime):
    from app.ui import runtime as module
    key = str(runtime.settings.database_path)
    module._instances[key] = runtime
    try:
        with pytest.raises(ValueError, match='Restart'):
            module.get_runtime(runtime.settings.model_copy(update={'email_mode': 'live'}))
    finally:
        module._instances.pop(key, None)


def test_baseline_editor_and_pdf_evidence_render(runtime, monkeypatch):
    p = project(runtime)
    draft = runtime.baselines.upload(p.project_id, text_pdf(['Evaluation criteria']), filename='proposal.pdf')
    runtime.baselines.edit(p.project_id, draft['id'], {'objectives': ['Evaluation'], 'milestones': [{'title': 'Prototype', 'completion_criteria': 'evaluation', 'deadline': '2026-12-01', 'source_pages': [1]}], 'warnings': []}, expected_revision=draft['revision'])
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Projects').run()
    assert not at.exception
    assert button(at, 'Confirm saved baseline').disabled
    next(c for c in at.checkbox if c.label.startswith('I checked the saved')).check().run()
    button(at, 'Confirm saved baseline').click().run()
    assert not at.exception
    assert p.project()['baseline_confirmed_at']
    at.sidebar.radio[0].set_value('History').run()
    assert not at.exception
    assert len(at.get('download_button')) == 1


def test_deadline_approval_ui_retains_history(runtime, monkeypatch):
    p = project(runtime)
    with runtime.db.transaction() as conn:
        conn.execute("INSERT INTO milestones(id,project_id,title,completion_criteria) VALUES ('m',?,'Prototype','evaluation')", (p.project_id,))
        conn.execute("INSERT INTO deadline_versions(id,project_id,milestone_id,due_date,status,source,decided_by) VALUES ('d',?,'m','2026-01-01','approved','baseline','supervisor')", (p.project_id,))
    from app.orchestrator.deadlines import DeadlineService
    key = DeadlineService(runtime.db).propose(p.project_id, 'm', '2026-12-01', source='email:fixture')
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Approvals').run()
    button(at, 'Approve deadline').click().run()
    assert not at.exception
    with runtime.db.transaction(write=False) as conn:
        statuses = {r['id']: r['status'] for r in conn.execute('SELECT * FROM deadline_versions WHERE project_id=?', (p.project_id,))}
    assert statuses == {'d': 'superseded', key: 'approved'}


def test_ui_monitoring_is_persistent_and_opt_in(runtime, monkeypatch):
    p = project(runtime)
    at = app(monkeypatch, runtime)
    at.sidebar.radio[0].set_value('Review').run()
    assert not next(c for c in at.checkbox if c.label.startswith('Monitor pushes')).value
    next(c for c in at.checkbox if c.label.startswith('Monitor pushes')).check()
    button(at, 'Save monitoring settings').click().run()
    with runtime.db.transaction(write=False) as conn:
        control = conn.execute('SELECT * FROM ui_controls WHERE project_id=?', (p.project_id,)).fetchone()
    assert control['monitoring'] == 1 and control['replies_enabled'] == 0


def test_smtp_check_never_sends(runtime, monkeypatch):
    import smtplib
    calls = []
    class Probe:
        def ehlo(self): calls.append('ehlo')
        def starttls(self, **kwargs): calls.append('tls')
        def close(self): calls.append('close')
        def send_message(self, *a, **kw): raise AssertionError('Probe must not send')
    monkeypatch.setattr(smtplib, 'SMTP', lambda *a, **kw: Probe())
    pid = project(runtime).project_id
    runtime.submit(pid, 'smtp_check')
    runtime.tick()
    assert calls == ['ehlo', 'tls', 'ehlo', 'close']
