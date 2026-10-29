"""Local single-supervisor application. Persistent state lives in SQLite."""
from datetime import date
import json
import re

import streamlit as st
from pydantic import ValidationError

from app.config import Settings
from app.db import ProjectStore
from app.orchestrator.deadlines import DeadlineService
from app.orchestrator.email import EmailService
from app.integrations.sheets.register import HEADERS, register_rows
from app.ui.runtime import get_runtime


PAGES = ['Dashboard', 'Setup', 'Projects', 'Review', 'Approvals', 'History', 'Register', 'Audit']


def rows(rt, sql, params=()):
    with rt.db.transaction(write=False) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def action(callback):
    try:
        result = callback()
    except ValidationError as error:
        st.error('Check these fields: ' + ', '.join('.'.join(map(str, e['loc'])) for e in error.errors(include_input=False)))
        return None
    except (ValueError, LookupError) as error:
        # These services use fixed validation messages; never display provider exceptions.
        st.error(str(error))
        return None
    except Exception:
        st.error('Action could not complete. Check saved task status and configuration.')
        return None
    st.success('Saved. Refresh to see the latest state.')
    return result


def enqueue(rt, pid, kind, payload=None, blob=None):
    return action(lambda: rt.submit(pid, kind, payload, blob))


@st.fragment(run_every=2)
def task_status(rt, pid):
    tasks = rows(rt, 'SELECT id,kind,status,error,created_at,result_json FROM ui_tasks WHERE project_id=? ORDER BY rowid DESC LIMIT 12', (pid,))
    if tasks:
        with st.expander('Background tasks', expanded=any(t['status'] in {'queued', 'running', 'failed', 'interrupted'} for t in tasks)):
            st.dataframe([{k: v for k, v in t.items() if k != 'result_json'} for t in tasks], hide_index=True)
            for task in tasks:
                if task['result_json']:
                    with st.expander(task['kind'] + ' result · ' + task['id'][:8]):
                        st.caption('Saved result when this action finished; this is not the current review or baseline status.')
                        value = json.loads(task['result_json'])
                        if isinstance(value, (dict, list)):
                            st.json(value)
                        else:
                            st.text(str(value))
                if task['status'] == 'interrupted':
                    st.warning('An action was interrupted. Check its saved result before retrying. For email, use delivery reconciliation below.')


def setup(rt, pid):
    st.header('Setup')
    cfg = rt.settings
    st.write('Integration configuration is read from your local .env. Restart Streamlit after changing it.')
    st.dataframe([
        {'Connection': 'GitHub', 'Mode / status': cfg.github_mode, 'Configured': bool(cfg.github_token.get_secret_value()) or cfg.github_mode == 'fake'},
        {'Connection': 'LLM', 'Mode / status': cfg.llm_mode, 'Configured': bool(cfg.groq_api_key.get_secret_value()) or cfg.llm_mode == 'fake'},
        {'Connection': 'Email delivery', 'Mode / status': cfg.email_mode + ' / ' + cfg.email_sender, 'Configured': bool(cfg.smtp_host)},
        {'Connection': 'Gmail replies', 'Mode / status': 'Read-only OAuth', 'Configured': (cfg.token_dir / 'gmail-read.json').exists()},
        {'Connection': 'Sheets', 'Mode / status': 'Explicit sync', 'Configured': bool(cfg.google_sheet_id) and (cfg.token_dir / 'sheets.json').exists()},
    ], hide_index=True)
    st.caption('Configured means settings/token presence, not a successful live connection.')
    st.info('Email test mode redirects actual delivery. Fake LLM/GitHub modes do not simulate SMTP or Google.')
    st.text('Current test destination: ' + cfg.test_redirect_email)
    st.code('python -m scripts.authorize_gmail read\npython -m scripts.authorize_gmail send\npython -m scripts.authorize_sheets', language='powershell')
    st.caption('Authorize only the Google services you need. These commands open the local browser; credentials stay outside the source tree.')
    if pid:
        if st.button('Check Gmail read connection'): enqueue(rt, pid, 'google_check')
        if st.button('Check SMTP connection (no email)'): enqueue(rt, pid, 'smtp_check')
        if st.button('Verify selected repository access'): enqueue(rt, pid, 'verify')
        task_status(rt, pid)
    else:
        st.info('Add a project to check its repository access and track connection tasks.')
    if rt.last_error: st.error(rt.last_error)


def dashboard(rt):
    st.header('Project dashboard')
    projects = ProjectStore.list_projects(rt.db)
    cells = register_rows(rt.db)
    metrics = st.columns(4)
    metrics[0].metric('Projects', len(projects))
    metrics[1].metric('Active', sum(p['status'] == 'active' for p in projects))
    metrics[2].metric('Delayed milestones', sum(int(r[7]) for r in cells.values()))
    metrics[3].metric('Open feedback', sum(int(r[8]) for r in cells.values()))
    if not projects:
        st.info('Start in Projects: add a student and repository, then upload and confirm the proposal baseline.')
        return
    entries = []
    for p in projects:
        item = dict(zip(HEADERS, cells[p['id']]))
        state = rows(rt, 'SELECT last_push_at FROM github_state WHERE project_id=?', (p['id'],))
        item['Last push'] = state[0]['last_push_at'] if state else None
        entries.append(item)
    st.dataframe(entries, hide_index=True)


def projects_page(rt, pid):
    st.header('Projects')
    with st.expander('Add project', expanded=pid is None):
        with st.form('new-project'):
            name = st.text_input('Student name')
            email = st.text_input('Student email')
            repo = st.text_input('GitHub repository', placeholder='owner/repository')
            create = st.form_submit_button('Create project')
        if create:
            project = action(lambda: ProjectStore.create(rt.db, name, email, repo))
            if project: st.rerun()
    if not pid: return
    project = ProjectStore(rt.db, pid).project()
    st.subheader('1. Student and repository')
    with st.form('edit-project-' + pid):
        name = st.text_input('Name', project['student_name'])
        email = st.text_input('Registered email', project['student_email'])
        repo = st.text_input('Repository', project['repo_full_name'], disabled=bool(project['baseline_confirmed_at']))
        paused = st.checkbox('Pause project', value=project['status'] == 'paused')
        edit = st.form_submit_button('Save project details')
    if edit: action(lambda: rt.edit_project(pid, name, email, paused=paused, repo=repo))
    st.caption('Changing an email address clears approval on unsent drafts. Resuming a paused project requires access verification.')
    st.subheader('2. Proposal and baseline')
    if project['baseline_confirmed_at']:
        baseline = rt.baselines.baseline(pid)
        st.success('Baseline confirmed and saved.')
        st.text('\n'.join(baseline['objectives']))
        st.dataframe(baseline['milestones'], hide_index=True)
    else:
        upload = st.file_uploader('Proposal PDF', type=['pdf'], key='proposal-' + pid)
        if st.button('Save proposal', disabled=upload is None):
            enqueue(rt, pid, 'proposal', {'filename': upload.name}, upload.getvalue())
        drafts = rt.baselines.drafts(pid)
        if drafts:
            draft_id = st.selectbox('Proposal draft', [d['id'] for d in drafts], format_func=lambda key: next(d['status'] + ' · ' + key[:8] for d in drafts if d['id'] == key))
            draft = rt.baselines.get(pid, draft_id)
            if draft['status'] == 'uploaded' and st.button('Extract proposed baseline'):
                enqueue(rt, pid, 'extract', {'id': draft_id})
            if draft['status'] == 'pending' and draft['job_id'] and draft.get('reason') not in {'invalid_output', 'retries_exhausted', 'unsupported_source_pages'}:
                st.caption('Retry sends the saved proposal excerpt to the configured LLM. Existing budgets are retained.')
                if st.button('Retry proposal extraction'):
                    enqueue(rt, pid, 'extract', {'id': draft_id})
            st.caption('Draft status: ' + draft['status'])
            if draft.get('reason'):
                explanations = {
                    'provider_http_error': 'Groq rejected the previous request. After restarting with the updated settings, retry extraction for a more specific diagnostic.',
                    'provider_request_error': 'Groq rejected the request (HTTP 400). Check model compatibility and request settings.',
                    'provider_auth_error': 'Groq rejected the API key. Check GROQ_API_KEY and restart the app.',
                    'provider_access_error': 'Groq denied access. Check account and model permissions.',
                    'provider_model_error': 'Groq could not use the selected model. Check its availability for your account.',
                    'provider_json_error': 'Groq could not generate valid JSON within this request.',
                    'invalid_output': 'The bounded JSON repair allowance is exhausted. Complete the baseline manually; repeated retries will not reset it.',
                }
                st.warning(explanations.get(draft['reason'], draft['reason']))
            if draft['limited_coverage']: st.warning('The extraction has limited coverage. Check the proposal and complete the baseline manually.')
            if draft['status'] not in {'confirmed', 'superseded', 'extracting'}:
                value = draft['draft']
                table = [{'title': m['title'], 'completion_criteria': m['completion_criteria'], 'deadline': m['deadline'] or ''} for m in value['milestones']]
                with st.form('baseline-' + draft_id + '-' + str(draft['revision'])):
                    objectives = st.text_area('Objectives (one per line)', '\n'.join(value['objectives']))
                    milestones = st.data_editor(table or [{'title': '', 'completion_criteria': '', 'deadline': ''}], num_rows='dynamic', hide_index=True,
                        column_config={'title': 'Milestone', 'completion_criteria': 'Completion criteria', 'deadline': 'Deadline (YYYY-MM-DD)'})
                    save = st.form_submit_button('Save baseline edits')
                if save:
                    edited = {'objectives': [s.strip() for s in objectives.splitlines() if s.strip()], 'warnings': value['warnings'],
                              'milestones': [{'title': m['title'], 'completion_criteria': m['completion_criteria'], 'deadline': m['deadline'] or None, 'source_pages': []}
                                             for m in milestones if any(m.values())]}
                    if action(lambda: rt.baselines.edit(pid, draft_id, edited, expected_revision=draft['revision'])): st.rerun()
                with st.expander('Saved baseline to confirm'): st.json(value)
                acknowledge = st.checkbox('I checked the saved objectives, criteria and dates, including any omitted proposal content.', key='ack-' + draft_id)
                if st.button('Confirm saved baseline', disabled=draft['status'] != 'editable' or not acknowledge):
                    if action(lambda: rt.baselines.confirm(pid, draft_id, expected_revision=draft['revision'], acknowledge_limited_coverage=acknowledge)): st.rerun()
    st.subheader('3. Repository access')
    st.text('Status: ' + project['status'])
    if rt.settings.github_mode == 'fake':
        st.caption('Fake repository access only. Sample commits are saved locally and replayed after restart.')
        if st.button('Create sample code commit'): enqueue(rt, pid, 'fake_push')
    if st.button('Find invitations'): enqueue(rt, pid, 'discover')
    for invitation in rows(rt, "SELECT invitation_id FROM github_invitations WHERE project_id=? AND status='pending'", (pid,)):
        if st.button('Accept invitation ' + str(invitation['invitation_id']), disabled=not bool(project['baseline_confirmed_at'])):
            enqueue(rt, pid, 'accept', {'id': invitation['invitation_id']})
    if st.button('Verify access', disabled=not bool(project['baseline_confirmed_at'])): enqueue(rt, pid, 'verify')
    task_status(rt, pid)


def evidence_view(rt, pid, ids, *, namespace='evidence'):
    for key in dict.fromkeys(ids):
        item = ProjectStore(rt.db, pid).evidence(key)
        if item:
            with st.expander(item['type'] + ' · ' + key[:8]):
                st.text(item['ref'])
                st.code(item['content'], language=None)
                if item['type'] == 'file_diff':
                    parts = item['ref'].split(':', 2)
                    if len(parts) == 3 and parts[0] == 'git' and re.fullmatch(r'[a-fA-F0-9]{40}', parts[1]):
                        repo = ProjectStore(rt.db, pid).project()['repo_full_name']
                        st.link_button('Open commit on GitHub', 'https://github.com/' + repo + '/commit/' + parts[1])
                elif item['type'] == 'pdf_page':
                    meta = json.loads(item['meta_json'])
                    if meta.get('document_id'):
                        doc = rt.pdfs.get(pid, meta['document_id'])
                        st.caption('Page ' + str(meta.get('page', '?')))
                        st.download_button('Download source PDF', rt.pdfs.bytes(pid, doc['id']), doc['filename'], mime='application/pdf', key=namespace + '-pdf-' + key)


@st.fragment(run_every=2)
def cycle_progress(rt, pid, cid):
    cycle = rt.engine.status(pid, cid)
    status = 'error' if cycle['status'] in {'failed', 'pending_evidence_review'} else 'complete' if cycle['status'] in {'completed', 'skipped'} else 'running'
    with st.status('Review: ' + cycle['stage'], state=status, expanded=True):
        st.write('State: ' + cycle['status'])
        payload = cycle['payload']
        if payload.get('repo_batches'):
            st.write(f"Repository batches reviewed: {payload.get('repo_batch_index', 0)} / {len(payload['repo_batches'])}")
        if payload.get('next_cycle'):
            st.info('This portion is complete. Select the newest continuation cycle to see the remaining review.')
        if cycle['reason']:
            explanations = {
                'review_batch_complete': 'This cycle has reached its call allowance. Continue in a new bounded cycle; completed batches and daily usage are preserved.',
                'batch_calls': 'The shared review batch has reached its call limit. Enter a new Review batch identifier above, then Resume saved review. Completed work and daily usage are preserved.',
                'daily_tokens': 'The daily project quota is exhausted. Wait for the UTC quota reset shown in the sidebar, then Resume saved review. Completed batches are saved.',
                'review_context_too_large': 'The milestone/feedback context or evidence metadata cannot fit even a single excerpt. Review the saved criteria and configured input limit.',
                'invalid_review_evidence': 'The model returned findings without valid supplied evidence references, and its repair allowance is exhausted. No unsupported findings were saved.',
            }
            st.warning(explanations.get(cycle['reason'], cycle['reason']))
        st.dataframe(rows(rt, 'SELECT stage,created_at FROM checkpoints WHERE project_id=? AND cycle_id=? ORDER BY created_at,rowid', (pid, cid)), hide_index=True)


def review_page(rt, pid):
    st.header('Review')
    if not pid:
        st.info('Add or select a project first.')
        return
    control = rows(rt, 'SELECT * FROM ui_controls WHERE project_id=?', (pid,))
    batch = st.text_input('Review batch identifier', value=control[0]['batch_id'] if control else 'ui-' + date.today().isoformat())
    enabled = st.checkbox('Monitor pushes and process queued reviews', value=bool(control and control[0]['monitoring']))
    replies = st.checkbox('Also poll matching Gmail replies', value=bool(control and control[0]['replies_enabled']))
    if st.button('Save monitoring settings'): action(lambda: rt.monitoring(pid, enabled, batch, replies=replies))
    if control and control[0]['last_error']: st.warning(control[0]['last_error'])
    st.caption('The batch call limit is shared across projects with this identifier. Pending reviews require an explicit resume.')
    if st.button('Review now'): enqueue(rt, pid, 'review', {'batch_id': batch})
    report = st.file_uploader('New or revised report PDF', type=['pdf'], key='report-' + pid)
    if st.button('Save report', disabled=report is None): enqueue(rt, pid, 'report', {'filename': report.name}, report.getvalue())
    if st.button('Read matching Gmail replies'): enqueue(rt, pid, 'replies')
    cycles = rows(rt, "SELECT * FROM cycles WHERE project_id=? AND trigger!='proposal_upload' ORDER BY rowid DESC", (pid,))
    if cycles:
        cid = st.selectbox('Review cycle', [c['id'] for c in cycles], format_func=lambda key: next(c['status'] + ' · ' + c['trigger'] + ' · ' + key[:8] for c in cycles if c['id'] == key))
        cycle = rt.engine.status(pid, cid)
        cycle_progress(rt, pid, cid)
        label = 'Continue next review batch' if cycle.get('reason') == 'review_batch_complete' else 'Resume saved review'
        if cycle['status'] == 'pending_evidence_review' and st.button(label):
            enqueue(rt, pid, 'resume', {'id': cid, 'batch_id': batch})
        group = cycle['payload'].get('review_cycles', [cid])
        marks = ','.join('?' for _ in group)
        for finding in rows(rt, f'SELECT * FROM findings WHERE project_id=? AND cycle_id IN ({marks})', (pid, *group)):
            st.text(finding['severity'].upper() + ': ' + finding['text'])
            evidence_view(rt, pid, json.loads(finding['evidence_ids_json']), namespace='finding-' + finding['id'])
    task_status(rt, pid)


def approvals(rt, pid):
    st.header('Approvals')
    if not pid:
        st.info('Select a project to review its drafts and proposed deadlines.')
        return
    service = EmailService(rt.db, rt.settings, None)
    drafts = rows(rt, "SELECT * FROM email_drafts WHERE project_id=? ORDER BY rowid DESC", (pid,))
    for draft in drafts:
        key = draft['id']
        with st.expander('Email · ' + draft['status'] + ' · ' + key[:8], expanded=draft['status'] in {'pending', 'approved', 'failed'}):
            st.text('Registered recipient: ' + draft['to_addr'])
            st.text('Delivery recipient: ' + (rt.settings.test_redirect_email if rt.settings.email_mode == 'test' else draft['to_addr']))
            st.caption('Delivery mode: ' + rt.settings.email_mode + ' / ' + rt.settings.email_sender)
            st.text('Message-ID: ' + draft['message_id'])
            if draft['delivery_uncertain']:
                st.warning('Delivery is uncertain. Check the sent mailbox/provider logs before recording a result. Do not resend without resolving it.')
                st.text(draft['subject'] + '\n\n' + draft['body'])
                note = st.text_input('Reconciliation note', key='note-' + key)
                decision = st.radio('Verified delivery result', ['Unresolved', 'Delivered', 'Not delivered'], key='resolution-' + key)
                if st.button('Record delivery resolution', key='resolve-' + key, disabled=decision == 'Unresolved' or not note.strip()):
                    action(lambda: service.resolve(pid, key, expected_revision=draft['revision'], delivered=decision == 'Delivered', note=note))
            elif draft['status'] == 'sent':
                st.text(draft['subject'] + '\n\n' + draft['body'])
                st.caption('Accepted at: ' + str(draft['sent_at']))
            else:
                with st.form('email-' + key + '-' + str(draft['revision'])):
                    subject = st.text_input('Subject', draft['subject'])
                    body = st.text_area('Email body', draft['body'], height=180)
                    save = st.form_submit_button('Save edits')
                    approve = st.form_submit_button('Approve this text')
                    reject = st.form_submit_button('Reject draft')
                def decide_email():
                    row = draft
                    if subject != draft['subject'] or body != draft['body']:
                        row = service.edit(pid, key, expected_revision=draft['revision'], subject=subject, body=body)
                    if approve or reject:
                        return service.decide(pid, key, expected_revision=row['revision'], approve=approve)
                    return row
                if save or approve or reject: action(decide_email)
                if draft['status'] == 'approved':
                    st.caption('This action sends real email, including when redirected in test mode.')
                    if st.button('Send approved email', key='send-' + key): enqueue(rt, pid, 'send', {'id': key})
    st.subheader('Proposed deadline changes')
    deadlines = DeadlineService(rt.db)
    for proposal in rows(rt, "SELECT d.*,m.title FROM deadline_versions d JOIN milestones m ON m.project_id=d.project_id AND m.id=d.milestone_id WHERE d.project_id=? AND d.status='proposed'", (pid,)):
        st.text(proposal['title'] + ' → ' + proposal['due_date'])
        st.caption('Source: ' + proposal['source'])
        if st.button('Approve deadline', key='yes-' + proposal['id']): action(lambda: deadlines.decide(pid, proposal['id'], approve=True))
        if st.button('Reject deadline', key='no-' + proposal['id']): action(lambda: deadlines.decide(pid, proposal['id'], approve=False))
    task_status(rt, pid)


def history(rt, pid):
    st.header('History')
    if not pid: return
    st.subheader('Deadlines and decisions')
    st.dataframe(rows(rt, 'SELECT * FROM deadline_versions WHERE project_id=? ORDER BY created_at', (pid,)), hide_index=True)
    st.dataframe(rows(rt, 'SELECT kind,target_id,decision,actor,created_at FROM supervisor_decisions WHERE project_id=? ORDER BY created_at DESC', (pid,)), hide_index=True)
    st.subheader('Feedback tracker')
    for item in rows(rt, 'SELECT * FROM feedback_items WHERE project_id=? ORDER BY rowid DESC', (pid,)):
        st.text(item['status'] + ': ' + item['text'])
        evidence_view(rt, pid, json.loads(item['evidence_ids_json']), namespace='feedback-' + item['id'])
    st.subheader('Student claims')
    st.dataframe(rows(rt, 'SELECT text,status,milestone_id FROM claims WHERE project_id=?', (pid,)), hide_index=True)
    st.subheader('Confirm milestone completion')
    milestones = rows(rt, 'SELECT id,title FROM milestones WHERE project_id=?', (pid,))
    if milestones:
        with st.expander('Propose a deadline change'):
            with st.form('manual-deadline-' + pid):
                target = st.selectbox('Milestone to reschedule', [r['id'] for r in milestones], format_func=lambda key: next(r['title'] for r in milestones if r['id'] == key))
                due = st.date_input('Proposed due date')
                submit = st.form_submit_button('Save deadline proposal')
            if submit:
                from app.db.repository import new_id
                action(lambda: DeadlineService(rt.db).propose(pid, target, due.isoformat(), source='supervisor:' + new_id()))
                st.caption('Approve or reject the saved proposal in Approvals.')
    evidence = rows(rt, "SELECT id,type,ref FROM evidence WHERE project_id=? AND type IN ('file_diff','pdf_page')", (pid,))
    if milestones and evidence:
        mid = st.selectbox('Milestone', [r['id'] for r in milestones], format_func=lambda key: next(r['title'] for r in milestones if r['id'] == key))
        eid = st.selectbox('Completion evidence', [r['id'] for r in evidence], format_func=lambda key: next(r['ref'] for r in evidence if r['id'] == key))
        evidence_view(rt, pid, [eid])
        ack = st.checkbox('I verified that this evidence satisfies the saved milestone criteria.')
        if st.button('Record confirmed completion', disabled=not ack): action(lambda: DeadlineService(rt.db).confirm_completion(pid, mid, eid))


def register(rt, pid):
    st.header('Progress register')
    st.dataframe([dict(zip(HEADERS, r)) for r in register_rows(rt.db).values()], hide_index=True)
    st.caption('Sheets sync writes generated columns A–L for all projects. Supervisor columns M onward are preserved. Conflicting generated cells are not overwritten.')
    if pid:
        if st.button('Sync all projects to configured Google Sheet', disabled=not bool(rt.settings.google_sheet_id)): enqueue(rt, pid, 'sheets')
        if st.button('Prepare Excel download'): enqueue(rt, pid, 'export')
        downloads = rows(rt, "SELECT id,output_blob FROM ui_tasks WHERE project_id=? AND kind='export' AND status='completed' ORDER BY rowid DESC LIMIT 1", (pid,))
        if downloads:
            st.download_button('Download progress.xlsx', downloads[0]['output_blob'], 'progress.xlsx', mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        st.dataframe(rows(rt, 'SELECT project_id,sheet_row,last_synced,last_error FROM register_sync'), hide_index=True)
        task_status(rt, pid)


def audit(rt, pid):
    st.header('Audit log')
    if pid:
        st.dataframe(rows(rt, 'SELECT event,actor,metadata_json,created_at FROM audit_log WHERE project_id=? ORDER BY id DESC LIMIT 500', (pid,)), hide_index=True)
        st.subheader('LLM and tool usage')
        st.dataframe(rows(rt, 'SELECT agent,model,tokens_in,tokens_out,cache_hit,outcome,created_at FROM llm_calls WHERE project_id=? ORDER BY rowid DESC LIMIT 100', (pid,)), hide_index=True)
        st.dataframe(rows(rt, 'SELECT agent,tool,outcome,created_at FROM tool_calls WHERE project_id=? ORDER BY rowid DESC LIMIT 100', (pid,)), hide_index=True)


def main():
    st.set_page_config(page_title='SupervisorAI', page_icon='🎓', layout='wide')
    st.title('SupervisorAI')
    st.caption('Project evidence, supervisor decisions, and progress in one place.')
    try:
        settings = Settings()
    except ValidationError as error:
        st.error('Configuration needs attention before the workspace can start.')
        for item in error.errors(include_input=False):
            st.text('.'.join(map(str, item['loc'])) + ': ' + item['msg'])
        st.info('SMTP_FROM must be one bare email address, such as supervisor@example.com. Keep secrets only in .env or the private token directory.')
        if st.button('Reload configuration'): st.rerun()
        return
    try:
        rt = get_runtime(settings)
    except Exception:
        st.error('The workspace worker could not start or settings changed. Stop other SupervisorAI workers and restart Streamlit.')
        return
    projects = ProjectStore.list_projects(rt.db)
    st.sidebar.title('Workspace')
    page = st.sidebar.radio('Page', PAGES)
    pid = st.sidebar.selectbox('Project', [p['id'] for p in projects], format_func=lambda key: next(p['student_name'] + ' · ' + p['repo_full_name'] for p in projects if p['id'] == key)) if projects else None
    st.sidebar.caption(f'LLM: {settings.llm_mode} · GitHub: {settings.github_mode} · Email: {settings.email_mode}')
    if settings.llm_mode == 'fake' or settings.github_mode == 'fake':
        st.sidebar.info('Fake integrations produce sample data, not a live assessment.')
    if pid:
        status = rt.gateway.status(pid)
        st.sidebar.metric('Queued LLM jobs', status['queue_depth'])
        st.sidebar.metric('Daily tokens', f"{status['daily_tokens_used']} / {status['daily_token_limit']}")
        st.sidebar.caption(f"Quota resets in {int(status['quota_resets_in_seconds']//3600)}h {int(status['quota_resets_in_seconds']%3600//60)}m (UTC day)")
        st.sidebar.caption('Project queue: ' + json.dumps(status['project_jobs']))
    if st.sidebar.button('Refresh saved state'): st.rerun()
    st.sidebar.caption('Local single-supervisor workspace. Keep the server on 127.0.0.1.')
    if page == 'Dashboard': dashboard(rt)
    elif page == 'Setup': setup(rt, pid)
    elif page == 'Projects': projects_page(rt, pid)
    elif page == 'Review': review_page(rt, pid)
    elif page == 'Approvals': approvals(rt, pid)
    elif page == 'History': history(rt, pid)
    elif page == 'Register': register(rt, pid)
    elif page == 'Audit': audit(rt, pid)
