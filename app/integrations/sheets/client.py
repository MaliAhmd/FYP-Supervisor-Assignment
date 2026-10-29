"""Thin values-only Sheets adapter. It never clears, inserts, or deletes rows."""
from dataclasses import dataclass
import re
import threading


class SheetsError(RuntimeError):
    pass


def column_name(index):
    value = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def quoted_tab(name):
    if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid sheet tab name')
    return "'" + name.replace("'", "''") + "'"


@dataclass(frozen=True)
class SheetSnapshot:
    rows: list[list]
    row_capacity: int
    column_capacity: int


class SheetsClient:
    def __init__(self, service, settings):
        self.service, self.settings = service, settings
        self._lock = threading.Lock()

    def read(self, spreadsheet_id, tab):
        quoted_tab(tab)
        try:
            with self._lock:
                meta = self.service.spreadsheets().get(spreadsheetId=spreadsheet_id,
                    fields='sheets(properties(title,gridProperties(rowCount,columnCount)))').execute(num_retries=0)
                props = next(s['properties'] for s in meta['sheets'] if s['properties']['title'] == tab)
                grid = props['gridProperties']
                width, height = grid['columnCount'], grid['rowCount']
                if not 12 <= width <= self.settings.sheets_max_columns:
                    raise SheetsError('unsupported_sheet_width')
                result = self.service.spreadsheets().values().batchGet(spreadsheetId=spreadsheet_id,
                    ranges=[f'{quoted_tab(tab)}!A:{column_name(width)}'], majorDimension='ROWS',
                    valueRenderOption='FORMULA').execute(num_retries=0)
            ranges = result['valueRanges']
            if len(ranges) != 1:
                raise ValueError()
            rows = ranges[0].get('values', [])
            if not isinstance(rows, list) or len(rows) > self.settings.sheets_max_rows:
                raise SheetsError('sheet_row_limit')
            if any(not isinstance(r, list) or len(r) > width or any(type(c) not in {str, int, float, bool} for c in r) for r in rows):
                raise ValueError()
            return SheetSnapshot(rows, height, width)
        except SheetsError:
            raise
        except Exception:
            raise SheetsError('sheet_read_failed') from None

    def batch_write(self, spreadsheet_id, tab, updates):
        """Only exact generated ranges A:L, with strings submitted as RAW."""
        data = []
        for row, values in updates:
            if type(row) is not int or row < 1 or len(values) != 12 or any(not isinstance(v, str) for v in values):
                raise ValueError('Invalid generated row')
            data.append({'range': f'{quoted_tab(tab)}!A{row}:L{row}', 'majorDimension': 'ROWS', 'values': [values]})
        if not data:
            return
        try:
            with self._lock:
                result = self.service.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id,
                    body={'valueInputOption': 'RAW', 'data': data}).execute(num_retries=0)
            if result.get('totalUpdatedCells') != len(data) * 12:
                raise ValueError()
        except Exception:
            # May have been accepted. The caller persists pending hashes first.
            raise SheetsError('sheet_write_uncertain') from None
