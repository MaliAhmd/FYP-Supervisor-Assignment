"""Deterministic BM25 ranking with page/evidence IDs; no embeddings or LLM."""

from collections import Counter
from dataclasses import dataclass, asdict
import math
import re

from app.integrations.pdf.store import PDFStore


@dataclass(frozen=True)
class Passage:
    document_id: str
    page: int
    evidence_id: str
    ref: str
    start: int
    end: int
    text: str
    score: float = 0.0

    def as_dict(self):
        return asdict(self)


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


class Retriever:
    def __init__(self, store: PDFStore):
        self.store = store

    def passages(self, project_id: str, document_id: str) -> list[Passage]:
        result = []
        size = self.store.settings.pdf_passage_chars
        for page in self.store.pages(project_id, document_id):
            text, offset = page["content"], 0
            while offset < len(text):
                end = min(len(text), offset + size)
                if end < len(text):
                    boundary = max(text.rfind("\n", offset + size // 2, end), text.rfind(" ", offset + size // 2, end))
                    if boundary > offset:
                        end = boundary + 1
                value = text[offset:end]
                if value.strip():
                    result.append(Passage(document_id, page["page_number"], page["evidence_id"], page["ref"], offset, end, value))
                offset = end
        return result

    def retrieve(self, project_id: str, document_id: str, query: str, *, top_k: int | None = None) -> list[Passage]:
        limit = self.store.settings.report_top_k if top_k is None else top_k
        if limit < 1:
            raise ValueError("top_k must be positive")
        passages = self.passages(project_id, document_id)
        terms = set(tokens(query))
        if not terms:
            return passages[:limit]
        counts = [Counter(tokens(p.text)) for p in passages]
        if not counts:
            return []
        average = sum(sum(c.values()) for c in counts) / len(counts) or 1
        frequency = Counter(term for count in counts for term in count)
        scored = []
        for passage, count in zip(passages, counts):
            score = 0.0
            length = sum(count.values())
            for term in terms:
                if count[term]:
                    idf = math.log(1 + (len(counts) - frequency[term] + .5) / (frequency[term] + .5))
                    score += idf * (count[term] * 2.5) / (count[term] + 1.5 * (.25 + .75 * length / average))
            if score > 0:
                scored.append(Passage(**{**passage.as_dict(), "score": score}))
        return sorted(scored, key=lambda p: (-p.score, p.page, p.start))[:limit]
