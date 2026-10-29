import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.config import ROOT
from app.db import Database
from scripts.seed_demo import config, load_state, totals


def run_demo(directory, *args, env=None):
    return subprocess.run([sys.executable, '-m', 'scripts.seed_demo', '--directory', str(directory), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)


def test_full_demo_uses_new_process_and_completed_resume_is_noop(tmp_path):
    directory = tmp_path / 'complete'
    result = run_demo(directory)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((directory / 'summary.json').read_text())
    assert summary['real_process_restart'] is True
    assert summary['totals']['projects'] == 3
    assert summary['totals']['milestones'] == 6
    assert summary['totals']['email_drafts'] == 9
    assert summary['totals']['reports'] == 2
    assert all(p['completed_reviews'] == 3 for p in summary['projects'])
    assert (directory / 'progress.xlsx').read_bytes().startswith(b'PK')
    db = Database(directory / 'supervisorai.sqlite3')
    before = totals(db)
    repeated = run_demo(directory, '--resume')
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert totals(db) == before
    with db.transaction(write=False) as conn:
        assert conn.execute("SELECT count(*) FROM feedback_items WHERE status='resolved'").fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM email_attempts').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM llm_calls WHERE attempted=1').fetchone()[0] == 28
        assert conn.execute('SELECT count(*) FROM llm_calls WHERE cache_hit=1').fetchone()[0] == 2


def test_manual_stop_restart_checkpoint_and_default_caps(tmp_path):
    directory = tmp_path / 'manual'
    first = run_demo(directory, '--stop-after-2')
    assert first.returncode == 0, first.stdout + first.stderr
    state = load_state(directory)
    assert state['completed_round'] == 2
    db = Database(directory / 'supervisorai.sqlite3')
    assert totals(db)['email_drafts'] == 6
    assert not (directory / 'progress.xlsx').exists()
    second = run_demo(directory, '--resume')
    assert second.returncode == 0, second.stdout + second.stderr
    state = load_state(directory)
    assert state['completed_round'] == 3 and state['seed_process'] != state['resume_process']
    cfg = config(db.path)
    assert cfg.max_input_tokens == 3000 and cfg.max_llm_calls_per_cycle == 6
    assert cfg.max_llm_calls_per_batch == 25 and cfg.daily_token_quota == 30000
    with db.transaction(write=False) as conn:
        payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM review_state WHERE cycle_id IN (SELECT id FROM cycles WHERE trigger='push')")]
    assert all(len(p['memory']['milestones']) == 2 for p in payloads)


def test_live_environment_never_changes_demo_modes(tmp_path, monkeypatch):
    for key, value in {'LLM_MODE': 'live', 'GITHUB_MODE': 'live', 'EMAIL_MODE': 'live', 'SMTP_FROM': 'invalid', 'MAX_INPUT_TOKENS': '1', 'DATABASE_PATH': str(tmp_path / 'production.sqlite3')}.items():
        monkeypatch.setenv(key, value)
    cfg = config(tmp_path / 'demo.sqlite3')
    assert (cfg.llm_mode, cfg.github_mode, cfg.email_mode) == ('fake', 'fake', 'test')
    assert cfg.database_path == tmp_path / 'demo.sqlite3' and cfg.max_input_tokens == 3000
    assert not cfg.groq_api_key.get_secret_value() and not cfg.smtp_password.get_secret_value()


def test_existing_or_unrecognized_directories_are_not_overwritten(tmp_path):
    directory = tmp_path / 'existing'
    directory.mkdir()
    marker = directory / 'do-not-change.txt'
    marker.write_text('existing content')
    result = run_demo(directory)
    assert result.returncode != 0 and 'already exists' in result.stderr
    assert marker.read_text() == 'existing content'
    with pytest.raises(FileNotFoundError): load_state(directory)
    (directory / 'manifest.json').write_text(json.dumps({'identity': 'other-app'}))
    with pytest.raises(RuntimeError, match='unrecognized'): load_state(directory)
