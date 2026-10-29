"""Read/hash/plan/batch-write/recover without writing any manual columns."""
import json

from app.db.repository import new_id
from app.gateway.prompts import digest
from app.gateway.service import _WorkerLock
from app.integrations.sheets.client import SheetsError, quoted_tab
from app.integrations.sheets.register import HEADERS, register_rows


def generated(row):
    # Retain cell types: a human numeric value replacing generated text is an edit.
    return list(row[:12]) + [''] * max(0, 12 - len(row))


class RegisterSync:
    def __init__(self, db, settings, client):
        self.db, self.settings, self.client = db, settings, client
        if not settings.google_sheet_id.strip():
            raise ValueError('GOOGLE_SHEET_ID is required')
        quoted_tab(settings.google_sheet_tab)

    def _lock(self):
        return _WorkerLock(self.db.path.with_suffix('.register.lock'))

    def _read(self):
        snapshot = self.client.read(self.settings.google_sheet_id, self.settings.google_sheet_tab)
        if len(snapshot.rows) > self.settings.sheets_max_rows or not 12 <= snapshot.column_capacity <= self.settings.sheets_max_columns:
            raise SheetsError('sheet_size_limit')
        return snapshot

    def sync(self, *, today=None):
        lock = self._lock()
        try:
            return self._sync(today=today)
        finally:
            lock.close()

    def _sync(self, *, today):
        desired = register_rows(self.db, today=today)
        result = {'updated': [], 'unchanged': [], 'conflicts': {}, 'status': 'complete'}
        if not desired:
            return result
        sid, tab = self.settings.google_sheet_id, self.settings.google_sheet_tab
        with self.db.transaction(write=False) as conn:
            saved = {r['project_id']: dict(r) for r in conn.execute('SELECT * FROM register_sync')}
        if any(r['spreadsheet_id'] != sid or r['tab_name'] != tab for r in saved.values()):
            raise SheetsError('register_destination_changed_or_legacy_unbound')
        snapshot = self._read()
        rows = snapshot.rows
        header = generated(rows[0] if rows else [])
        updates = []
        if header != HEADERS:
            if any(v != '' for v in header) or any(any(v != '' for v in generated(r)) for r in rows[1:]):
                raise SheetsError('generated_headers_changed')
            updates.append((1, HEADERS))
        positions = {}
        for index, row in enumerate(rows[1:], 2):
            if row and isinstance(row[0], str) and row[0]:
                positions.setdefault(row[0], []).append(index)
        next_row = max(2, len(rows) + 1)
        plans = {}
        for pid, values in desired.items():
            previous = saved.get(pid)
            locations = positions.get(pid, [])
            if len(locations) > 1:
                result['conflicts'][pid] = 'duplicate_project_id'
                continue
            if locations:
                row = locations[0]
                current = generated(rows[row - 1])
                current_hash = digest(current)
                allowed = {digest(values)} if not previous else {previous['generated_hash'], previous['pending_hash']}
                if current_hash not in allowed:
                    result['conflicts'][pid] = 'generated_cells_edited'
                    continue
            elif previous:
                # A never-acknowledged new row may still be blank after a failed write.
                row = previous['sheet_row']
                raw = rows[row - 1] if row <= len(rows) else []
                if previous['generated_hash'] is not None or previous['pending_hash'] is None or any(v != '' for v in raw):
                    result['conflicts'][pid] = 'project_id_missing'
                    continue
                current, current_hash = [''] * 12, None
            else:
                row = next_row
                next_row += 1
                current, current_hash = [''] * 12, None
            if row > min(snapshot.row_capacity, self.settings.sheets_max_rows):
                result['conflicts'][pid] = 'sheet_row_capacity'
                continue
            # Pending blank rows may occupy slots after the last populated row.
            if any(plan['row'] == row for plan in plans.values()):
                result['conflicts'][pid] = 'row_reservation_collision'
                continue
            plans[pid] = {'row': row, 'values': values, 'hash': digest(values), 'current_hash': current_hash}
            if current == values:
                result['unchanged'].append(pid)
            else:
                updates.append((row, values))
                result['updated'].append(pid)
            next_row = max(next_row, row + 1)
        # Reserve before remote I/O. Pending hashes recover success after a lost response.
        with self.db.transaction() as conn:
            for pid, reason in result['conflicts'].items():
                conn.execute('UPDATE register_sync SET last_error=? WHERE project_id=?', (reason, pid))
                conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'register.conflict','application',?)", (pid, json.dumps({'reason': reason})))
            for pid, plan in plans.items():
                conn.execute("""INSERT INTO register_sync(project_id,sheet_row,spreadsheet_id,tab_name,generated_hash,pending_hash,pending_values)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET sheet_row=excluded.sheet_row,
                    generated_hash=excluded.generated_hash,pending_hash=excluded.pending_hash,pending_values=excluded.pending_values,last_error=NULL""",
                    (pid, plan['row'], sid, tab, plan['current_hash'], plan['hash'], json.dumps(plan['values'])))
        if updates:
            try:
                self.client.batch_write(sid, tab, updates)
            except Exception:
                with self.db.transaction() as conn:
                    for pid in plans:
                        conn.execute("UPDATE register_sync SET last_error='sheet_write_uncertain' WHERE project_id=?", (pid,))
                return {**result, 'status': 'pending', 'reason': 'sheet_write_uncertain', 'updated': []}
        with self.db.transaction() as conn:
            for pid, plan in plans.items():
                conn.execute("""UPDATE register_sync SET generated_hash=?,pending_hash=NULL,pending_values=NULL,last_error=NULL,
                    last_synced=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=?""", (plan['hash'], pid))
                if pid in result['updated']:
                    conn.execute("INSERT INTO audit_log(project_id,event,actor,metadata_json) VALUES (?,'register.synced','application',?)", (pid, json.dumps({'row': plan['row']})))
        return result

    def acknowledge_generated_edit(self, pid, *, observed_hash):
        """Supervisor explicitly allows regeneration after inspecting this exact row.

        No remote write occurs here. A later sync regenerates A:L; M onward is
        untouched. A changed observation must be reviewed again.
        """
        lock = self._lock()
        try:
            snapshot = self._read()
            if not snapshot.rows or generated(snapshot.rows[0]) != HEADERS:
                raise SheetsError('generated_headers_changed')
            found = [(i, generated(r)) for i, r in enumerate(snapshot.rows[1:], 2) if r and r[0] == pid]
            if len(found) != 1 or digest(found[0][1]) != observed_hash:
                raise ValueError('Row changed or is ambiguous; review again')
            with self.db.transaction() as conn:
                previous = conn.execute('SELECT * FROM register_sync WHERE project_id=?', (pid,)).fetchone()
                if not previous or previous['spreadsheet_id'] != self.settings.google_sheet_id or previous['tab_name'] != self.settings.google_sheet_tab:
                    raise ValueError('Project is not bound to this register')
                conn.execute('UPDATE register_sync SET sheet_row=?,generated_hash=?,pending_hash=NULL,pending_values=NULL,last_error=NULL WHERE project_id=?', (found[0][0], observed_hash, pid))
                conn.execute("INSERT INTO supervisor_decisions(id,project_id,kind,target_id,decision,actor,payload_json) VALUES (?,?,'register',?,'allow_regeneration','supervisor',?)", (new_id(), pid, pid, json.dumps({'observed_hash': observed_hash})))
        finally:
            lock.close()
