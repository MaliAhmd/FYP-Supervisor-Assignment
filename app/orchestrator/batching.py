"""Deterministic request packing; every excerpt remains project-owned evidence."""
import json
from hashlib import sha256

from app.db import ProjectStore
from app.gateway.prompts import messages, estimate_tokens
from app.gateway.types import BudgetExceeded


FIELDS = ('id', 'type', 'ref', 'content_hash', 'content', 'meta_json')


def fits(gateway, pid, cid, batch_id, agent, data, ids):
    store = ProjectStore(gateway.db, pid)
    evidence = []
    for key in ids:
        item = store.evidence(key)
        if item is None:
            raise ValueError('Evidence does not belong to project')
        evidence.append({k: item[k] for k in FIELDS})
    remaining = gateway.ledger.remaining({'project_id': pid, 'cycle_id': cid,
                                         'batch_id': batch_id, 'agent': agent})
    chat = messages(gateway.specs[agent], {'data': data, 'evidence': evidence}, remaining,
                    gateway.settings.max_output_tokens, repair=bool(gateway.settings.json_repair_retries))
    return estimate_tokens(chat) <= gateway.settings.max_input_tokens - 64


def pack_repository(gateway, pid, cid, batch_id, data, ids):
    store = ProjectStore(gateway.db, pid)
    def accepts(keys):
        return fits(gateway, pid, cid, batch_id, 'repo_reviewer', data, keys)
    if not accepts([]):
        raise BudgetExceeded('review_context_too_large')
    parts = []
    for key in ids:
        item = store.evidence(key)
        if item is None:
            raise ValueError('Evidence does not belong to project')
        if accepts([key]):
            parts.append(key)
            continue
        # Trial payloads use conservative fixed-size placeholder IDs/hashes.
        # Persist only selected slices; no text is dropped or summarized.
        text, start = item['content'], 0
        while start < len(text):
            low, high, best = 1, min(len(text) - start, gateway.settings.max_diff_chars_per_file), 0
            while low <= high:
                size = (low + high) // 2
                # A temporary value is measured exactly like Gateway.submit.
                meta = {'source_id': key, 'start': start, 'end': start + size}
                excerpt = {'id': '0' * 32, 'type': item['type'],
                    'ref': item['ref'] + f':chars:{start}:{start + size}',
                    'content_hash': sha256(text[start:start + size].encode()).hexdigest(),
                    'content': text[start:start + size], 'meta_json': json.dumps(meta)}
                remaining = gateway.ledger.remaining({'project_id': pid, 'cycle_id': cid,
                    'batch_id': batch_id, 'agent': 'repo_reviewer'})
                chat = messages(gateway.specs['repo_reviewer'], {'data': data, 'evidence': [excerpt]},
                    remaining, gateway.settings.max_output_tokens, repair=bool(gateway.settings.json_repair_retries))
                if estimate_tokens(chat) <= gateway.settings.max_input_tokens - 64:
                    best, low = size, size + 1
                else:
                    high = size - 1
            if not best:
                raise BudgetExceeded('review_context_too_large')
            end = start + best
            part = store.add_evidence(cid, item['type'], item['ref'] + f':chars:{start}:{end}',
                                      text[start:end], {'source_id': key, 'start': start, 'end': end})
            parts.append(part)
            start = end
        if not text:
            raise BudgetExceeded('review_context_too_large')
    batches, current = [], []
    for key in parts:
        if current and not accepts(current + [key]):
            batches.append(current)
            current = []
        current.append(key)
    if current:
        batches.append(current)
    return batches
