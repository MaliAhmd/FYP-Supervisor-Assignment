"""Three fictional projects, three cycles each, with a real process restart.

Never uses .env, SMTP, OAuth, or a live provider. The destination must be new;
--resume only opens a directory bearing this demo's manifest.
"""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

from app.agents.review import all_specs
from app.config import ROOT, Settings
from app.db import Database, ProjectStore
from app.gateway import Gateway
from app.gateway.clients import FakeLLMClient
from app.gateway.types import CompletionResponse
from app.integrations.github.access import AccessService
from app.integrations.github.fake import FakeGitHubClient
from app.integrations.gmail.reader import InboundMessage
from app.integrations.pdf.store import PDFStore
from app.integrations.sheets.excel import export_excel
from app.integrations.sheets.fake import FakeSheetsClient
from app.orchestrator.baseline import BaselineService
from app.orchestrator.deadlines import DeadlineService
from app.orchestrator.email import EmailService
from app.orchestrator.review import ReviewEngine
from app.orchestrator.sheets import RegisterSync
from app.scheduler.queue import ReviewQueue
from app.scheduler.replies import ReplyPoller
from scripts.email_demo import RecordingSender
from scripts.pdf_fixtures import text_pdf


TODAY = date(2026, 10, 26)
IDENTITY = 'supervisorai-phase9-v1'
SCENARIOS = [('amina', 'Amina Example', 'delayed'), ('bilal', 'Bilal Example', 'reply'), ('chen', 'Chen Example', 'report')]


def require(condition, message):
    if not condition:
        raise RuntimeError('Demo verification failed: ' + message)


def config(path):
    # Explicit initial values outrank both environment variables and .env.
    values = {name: field.get_default(call_default_factory=True) for name, field in Settings.model_fields.items()}
    values.update(database_path=path, llm_mode='fake', github_mode='fake', email_mode='test',
                  google_sheet_id='phase9-fake-register', test_redirect_email='supervisor-demo@example.com',
                  llm_tokens_per_minute=100000, llm_requests_per_minute=1000)
    return Settings(_env_file=None, **values)


class ScenarioClient(FakeLLMClient):
    """Trusted scenario fixtures, not an agent that obeys evidence instructions."""
    def __init__(self, state, db):
        super().__init__()
        self.state, self.db = state, db

    def complete(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
        data, evidence = payload['data'], payload['evidence']
        record = next(p for p in self.state['projects'] if p['id'] == request.project_id)
        if request.agent == 'baseline':
            value = {'objectives': ['Build an evaluation prototype.'], 'milestones': [
                {'title': 'Prototype', 'completion_criteria': 'evaluation prototype', 'deadline': '2026-10-01', 'source_pages': [1]},
                {'title': 'Report', 'completion_criteria': 'evaluation report', 'deadline': '2026-12-01', 'source_pages': [1]}], 'warnings': []}
        elif request.agent == 'coordinator':
            value = {'actions': data['available']}
        elif request.agent in {'repo_reviewer', 'report_reviewer'}:
            ids = [e['id'] for e in evidence] or [p['evidence_id'] for p in data['passages']]
            previous = data['feedback']
            value = {'findings': [{'text': 'Add evaluation evidence.', 'severity': 'warning' if not previous and self.state['round'] == 1 else 'info', 'evidence_ids': ids[:1]}],
                     'feedback': [{'feedback_id': f['id'], 'status': 'resolved' if self.state['round'] == 3 else 'partial', 'evidence_ids': ids[:1]} for f in previous]}
        elif data['mode'] == 'analyze':
            require(record['scenario'] == 'reply', 'unexpected reply scenario')
            source = next(e['id'] for e in evidence if e['type'] == 'email')
            value = {'claims': [{'text': 'Prototype is complete; please extend its deadline.', 'milestone_id': record['prototype_id'],
                                  'source_id': source, 'completion': True, 'evidence_ids': [], 'due_date': '2026-11-30'}],
                     'narrative': 'Completion is unsupported. The date is only a proposal.'}
        else:
            value = {'subject': 'Evaluation evidence requested', 'body': 'Please supply reproducible evaluation evidence and address the saved feedback.'}
        return CompletionResponse(json.dumps(value), 100, 100)


class ScenarioEngine(ReviewEngine):
    """Fix the scenario's calendar date without changing the production clock."""
    def _memory(self, pid):
        result = super()._memory(pid)
        result['delayed'] = DeadlineService(self.db).delayed(pid, today=TODAY)
        return result

    def _input(self, pid, cid, stage, state):
        data, ids = super()._input(pid, cid, stage, state)
        if 'delayed' in data:
            data['delayed'] = DeadlineService(self.db).delayed(pid, today=TODAY)
        return data, ids


def save_state(directory, state):
    temporary = directory / 'manifest.tmp'
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    temporary.replace(directory / 'manifest.json')


def load_state(directory):
    state = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    require(state.get('identity') == IDENTITY, 'unrecognized directory')
    require(state.get('completed_round') in {2, 3}, 'only the completed round-two checkpoint can resume')
    require((directory / 'supervisorai.sqlite3').is_file(), 'checkpoint database is missing')
    return state


def rebuild_git(state):
    git = FakeGitHubClient()
    for p in state['projects']:
        git.add_repository(p['repo'])
        for commit in p['commits']:
            head = git.publish(p['repo'], commit['files'])
            require(head == commit['sha'], 'fake Git history changed during restart')
    return git


def publish(git, record, number):
    files = {'main.py': f'def evaluation():\n    return {number}\n'}
    head = git.publish(record['repo'], files)
    record['commits'].append({'files': files, 'sha': head})
    return head


def drive(engine, gateway, cycles, batch):
    """Round-robin orchestration; one shared batch for all three projects."""
    for _ in range(200):
        done = True
        for pid, cid in cycles:
            state = engine.step(pid, cid, batch_id=batch)
            require(state['status'] not in {'pending_evidence_review', 'failed'}, f"{state['stage']}: {state.get('reason')}")
            done &= state['status'] == 'completed'
        if done: return
        gateway.run_once()
    raise RuntimeError('Demo exceeded its bounded worker iterations')


def initialize(directory):
    directory.mkdir(parents=True, exist_ok=False)
    db = Database(directory / 'supervisorai.sqlite3')
    db.migrate()
    state = {'identity': IDENTITY, 'today': TODAY.isoformat(), 'seed_process': os.getpid(), 'completed_round': 0, 'round': 0, 'projects': []}
    for slug, name, scenario in SCENARIOS:
        p = ProjectStore.create(db, name, slug + '@example.com', 'fictional/' + slug + '-demo')
        state['projects'].append({'id': p.project_id, 'repo': p.project()['repo_full_name'], 'scenario': scenario, 'commits': [], 'cycles': []})
    cfg, git = config(db.path), rebuild_git(state)
    with Gateway(db, cfg, all_specs(), client=ScenarioClient(state, db), start=False) as gateway:
        pdfs = PDFStore(db, cfg)
        baseline = BaselineService(db, pdfs, gateway)
        for p in state['projects']:
            draft = baseline.upload(p['id'], text_pdf(['Evaluation prototype and report. Prototype due 2026-10-01; report due 2026-12-01.']))
            queued = baseline.request_extraction(p['id'], draft['id'], 'demo-onboarding')
            require(gateway.run_once(), 'baseline job not ready')
            edited = baseline.collect(p['id'], draft['id'])
            require(edited['status'] == 'editable', 'baseline extraction did not complete')
            saved = baseline.confirm(p['id'], draft['id'], expected_revision=edited['revision'], acknowledge_limited_coverage=True)
            p['prototype_id'] = next(m['id'] for m in saved['milestones'] if m['title'] == 'Prototype')
            p['baseline_draft'] = draft['id']
            # Explicit fictional collaborator acceptance, through the production service.
            invitation = len(git.pending_invitations) + len(git.accepted_ids) + 1
            git.invite(p['repo'], invitation)
            AccessService(db, git, git.accept_invitation).accept(p['id'], invitation)
    return db, state, git


def run_round(db, state, git, number):
    state['round'] = number
    cfg = config(db.path)
    pdfs = PDFStore(db, cfg)
    cycles = []
    for p in state['projects']:
        pid = p['id']
        if p['scenario'] == 'report' and number in {2, 3}:
            version = number - 1
            content = text_pdf(['Background introduction.', f'Evaluation report revision {version}: measured prototype results.'])
            document = pdfs.ingest(pid, content, kind='report', filename=f'report-v{version}.pdf')
            duplicate = pdfs.ingest(pid, content, kind='report', filename='duplicate.pdf')
            require(document['id'] == duplicate['id'], 'report upload was duplicated')
            cid = document['cycle_id']
        elif p['scenario'] == 'reply' and number == 2:
            with db.transaction(write=False) as conn:
                outgoing = conn.execute('SELECT * FROM email_drafts WHERE project_id=? AND cycle_id=?', (pid, p['cycles'][0])).fetchone()
            sender = RecordingSender()
            service = EmailService(db, cfg, sender)
            service.decide(pid, outgoing['id'], expected_revision=outgoing['revision'], approve=True)
            service.send(pid, outgoing['id'])
            service.send(pid, outgoing['id'])
            require(len(sender.calls) == 1, 'duplicate fake email delivery')
            reply = InboundMessage('demo-bilal-reply', cfg.test_redirect_email, '<bilal-reply@example.com>', outgoing['message_id'], (),
                                   '2026-10-26T12:00:00Z', 'The prototype is complete. Please move its deadline to 2026-11-30.')
            poller = ReplyPoller(db, cfg, None)
            require(poller.ingest(pid, reply), 'reply did not match its outgoing message')
            require(not poller.ingest(pid, reply), 'reply imported twice')
            with db.transaction(write=False) as conn:
                cid = conn.execute("SELECT id FROM cycles WHERE project_id=? AND trigger='email_reply'", (pid,)).fetchone()[0]
        else:
            head = publish(git, p, number)
            # Simulated time exercises the real default five-minute debounce without sleeping.
            now = [1793016000 + number * 1000]
            queue = ReviewQueue(db, cfg.debounce_seconds, clock=lambda: now[0])
            require(queue.observe_head(pid, head, source='manual'), 'new push not recorded')
            require(queue.flush(pid) is None, 'debounce was bypassed')
            now[0] += cfg.debounce_seconds
            cid = queue.flush(pid)
            require(cid is not None, 'push not queued after debounce')
            require(not queue.observe_head(pid, head, source='manual'), 'same head processed twice')
        p['cycles'].append(cid)
        cycles.append((pid, cid))
    with Gateway(db, cfg, all_specs(), client=ScenarioClient(state, db), start=False) as gateway:
        engine = ScenarioEngine(db, gateway, git, pdfs)
        drive(engine, gateway, cycles, f'demo-round-{number}')
        # Re-running finished jobs must not add calls, findings, or email drafts.
        before = totals(db)
        drive(engine, gateway, cycles, f'demo-round-{number}')
        require(totals(db) == before, 'completed-cycle replay changed stored state')
    if number == 2:
        p = next(p for p in state['projects'] if p['scenario'] == 'reply')
        with db.transaction(write=False) as conn:
            proposal = conn.execute("SELECT id FROM deadline_versions WHERE project_id=? AND status='proposed'", (p['id'],)).fetchone()
        require(proposal is not None, 'student reply did not create a proposed deadline')
        DeadlineService(db).decide(p['id'], proposal[0], approve=True)
        state['round_two_totals'] = totals(db)
        state['round_two_reviewed'] = {p['id']: ProjectStore(db, p['id']).project()['last_reviewed_sha'] for p in state['projects']}
    state['completed_round'] = number
    print(f'Round {number}: three reviews completed using saved baselines.', flush=True)


def totals(db):
    with db.transaction(write=False) as conn:
        return {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('projects', 'milestones', 'cycles', 'findings', 'email_drafts', 'llm_calls', 'reports', 'claims')}


def verify(db, state):
    expected = state['completed_round']
    cfg = config(db.path)
    summary = {'scenario_date': TODAY.isoformat(), 'completed_round': expected, 'totals': totals(db), 'projects': [],
               'real_process_restart': bool(state.get('resume_process') and state['resume_process'] != state['seed_process'])}
    with db.transaction(write=False) as conn:
        summary['fake_provider_attempts'] = conn.execute('SELECT count(*) FROM llm_calls WHERE attempted=1').fetchone()[0]
        summary['cache_hits'] = conn.execute('SELECT count(*) FROM llm_calls WHERE cache_hit=1').fetchone()[0]
        require(summary['totals']['projects'] == 3 and summary['totals']['milestones'] == 6, 'project/milestone counts')
        for p in state['projects']:
            pid = p['id']
            reviews = conn.execute("SELECT * FROM cycles WHERE project_id=? AND trigger!='proposal_upload' ORDER BY rowid", (pid,)).fetchall()
            require(len(reviews) == expected and all(c['status'] == 'completed' for c in reviews), 'review count/state')
            require(conn.execute("SELECT count(*) FROM llm_calls WHERE project_id=? AND agent='baseline' AND attempted=1", (pid,)).fetchone()[0] == 1, 'proposal extraction repeated')
            require(conn.execute('SELECT count(*) FROM email_drafts WHERE project_id=?', (pid,)).fetchone()[0] == expected, 'draft deduplication')
            for cycle in reviews:
                count = conn.execute('SELECT count(*) FROM llm_calls WHERE project_id=? AND cycle_id=? AND attempted=1', (pid, cycle['id'])).fetchone()[0]
                require(count <= cfg.max_llm_calls_per_cycle, 'cycle call budget')
            if expected == 3 and p['scenario'] != 'report':
                require(reviews[-1]['before_sha'] == state['round_two_reviewed'][pid], 'cycle three did not use saved reviewed SHA')
            if expected == 3:
                saved = conn.execute('SELECT payload FROM review_state WHERE project_id=? AND cycle_id=?', (pid, reviews[-1]['id'])).fetchone()
                memory = json.loads(saved[0])['memory']
                require(len(memory['milestones']) == 2 and memory['feedback'], 'cycle three did not load baseline/feedback memory')
                if p['scenario'] == 'reply':
                    require(next(m for m in memory['milestones'] if m['id'] == p['prototype_id'])['due_date'] == '2026-11-30', 'cycle three missed the approved deadline')
                    require(any(c['status'] == 'unsupported' for c in memory['claims']), 'cycle three missed the earlier claim')
            delayed = DeadlineService(db).delayed(pid, today=TODAY)
            summary['projects'].append({'id': pid, 'scenario': p['scenario'], 'completed_reviews': len(reviews), 'delayed_milestones': len(delayed)})
        require(next(p for p in summary['projects'] if p['scenario'] == 'delayed')['delayed_milestones'] == 1, 'delayed scenario')
        require(conn.execute("SELECT count(*) FROM claims WHERE status='unsupported'").fetchone()[0] == 1, 'unsupported claim')
        require(conn.execute("SELECT count(*) FROM deadline_versions WHERE status='superseded'").fetchone()[0] == 1, 'superseded deadline history')
        require(conn.execute("SELECT count(*) FROM deadline_versions WHERE status='approved' AND due_date='2026-11-30'").fetchone()[0] == 1, 'approved extension')
        require(conn.execute('SELECT count(*) FROM reports').fetchone()[0] == expected - 1, 'report revisions')
        for report in conn.execute('SELECT * FROM reports ORDER BY version'):
            require(len(json.loads(report['page_refs'])) == 2, 'page evidence missing')
        require(conn.execute('SELECT count(DISTINCT sha256) FROM reports').fetchone()[0] == expected - 1, 'report hash did not change')
        report_project = next(p for p in state['projects'] if p['scenario'] == 'report')
        for cid in report_project['cycles'][1:]:
            findings = conn.execute('SELECT evidence_ids_json FROM findings WHERE project_id=? AND cycle_id=?', (report_project['id'], cid)).fetchall()
            require(findings, 'report review has no findings')
            for row in findings:
                for eid in json.loads(row[0]):
                    item = conn.execute('SELECT type,meta_json FROM evidence WHERE project_id=? AND id=?', (report_project['id'], eid)).fetchone()
                    require(item['type'] == 'pdf_page' and json.loads(item['meta_json'])['page'] == 2, 'report citation did not point to relevant page two')
        require(not conn.execute('SELECT id FROM llm_calls WHERE attempted=1 AND agent=\'coordinator\' GROUP BY project_id,cycle_id HAVING count(*)>?', (cfg.max_coordinator_calls,)).fetchall(), 'coordinator budget')
        require(not conn.execute('SELECT batch_id FROM llm_calls WHERE attempted=1 GROUP BY batch_id HAVING count(*)>?', (cfg.max_llm_calls_per_batch,)).fetchall(), 'batch call budget')
        require(not conn.execute('SELECT project_id,cycle_id,agent FROM tool_calls GROUP BY project_id,cycle_id,agent HAVING count(*)>?', (cfg.max_specialist_tool_calls,)).fetchall(), 'tool budget')
    return summary


def resume(directory):
    state = load_state(directory)
    db = Database(directory / 'supervisorai.sqlite3')
    if state['completed_round'] == 3:
        summary = verify(db, state)
        print('Completed demo verified; no cycles, calls or drafts repeated.', flush=True)
        return summary
    require(totals(db) == state['round_two_totals'], 'checkpoint database changed before restart')
    state['resume_process'] = os.getpid()
    git = rebuild_git(state)
    run_round(db, state, git, 3)
    summary = verify(db, state)
    fake_sheet = FakeSheetsClient([[''] * 12 + ['Supervisor notes']])
    register = RegisterSync(db, config(db.path), fake_sheet)
    register.sync(today=TODAY)
    fake_sheet.rows[1].append('Manual supervisor note retained.')
    require(register.sync(today=TODAY)['updated'] == [], 'unchanged register rewritten')
    require(fake_sheet.rows[1][12] == 'Manual supervisor note retained.', 'manual note overwritten')
    export_excel(db, directory / 'progress.xlsx', today=TODAY)
    save_state(directory, state)
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)
    print('No live LLM, GitHub, Gmail, SMTP or Sheets service was called.', flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'data' / 'phase9_demo')
    parser.add_argument('--stop-after-2', action='store_true', help='Save a checkpoint and exit before round three')
    parser.add_argument('--resume', action='store_true', help='Resume only a recognized round-two demo checkpoint')
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.resume and args.stop_after_2:
        parser.error('Choose either --resume or --stop-after-2')
    if args.resume:
        resume(directory)
        return
    if directory.exists():
        parser.error('Destination already exists. Use --resume for a recognized demo, or choose a new directory.')
    db, state, git = initialize(directory)
    run_round(db, state, git, 1)
    run_round(db, state, git, 2)
    verify(db, state)
    save_state(directory, state)
    print('Stopped after round two. Proposal baselines and checkpoints are saved.', flush=True)
    if args.stop_after_2:
        return
    # A different interpreter has no old Python objects or fake-client memory.
    env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH'}}
    env['PYTHONIOENCODING'] = 'utf-8'
    subprocess.run([sys.executable, '-m', 'scripts.seed_demo', '--directory', str(directory), '--resume'],
                   cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
