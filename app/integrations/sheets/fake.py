"""Offline spreadsheet with retained manual cells and controllable lost responses."""
from copy import deepcopy
from app.integrations.sheets.client import SheetSnapshot, SheetsError


class FakeSheetsClient:
    def __init__(self, rows=(), *, row_capacity=1000, column_capacity=26):
        self.rows = deepcopy(list(rows))
        self.row_capacity, self.column_capacity = row_capacity, column_capacity
        self.writes, self.reads = [], 0
        self.fail_before_write = False
        self.fail_after_write = False

    def read(self, spreadsheet_id, tab):
        self.reads += 1
        return SheetSnapshot(deepcopy(self.rows), self.row_capacity, self.column_capacity)

    def batch_write(self, spreadsheet_id, tab, updates):
        if self.fail_before_write:
            raise SheetsError('offline_failure')
        self.writes.append(deepcopy(updates))
        for number, values in updates:
            if len(values) != 12 or number > self.row_capacity:
                raise AssertionError('Invalid generated range')
            while len(self.rows) < number:
                self.rows.append([])
            target = self.rows[number - 1]
            target.extend([''] * max(0, 12 - len(target)))
            target[:12] = values
        if self.fail_after_write:
            raise SheetsError('lost_response')
