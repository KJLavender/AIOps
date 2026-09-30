"""Phase 1 Knowledge Base: append-only JSONL with keyword/symptom scoring."""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from ..models import KBEntry, PodIssue
from ..util import tokenize
from .base import KnowledgeBase

log = logging.getLogger("aiops.kb")


class JsonlKnowledgeBase(KnowledgeBase):
    def __init__(self, path: str, score_threshold: float = 0.35) -> None:
        self.path = path
        self.score_threshold = score_threshold
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def _load(self) -> list[KBEntry]:
        if not os.path.exists(self.path):
            return []
        entries: list[KBEntry] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(KBEntry.from_dict(json.loads(line)))
                except json.JSONDecodeError:
                    log.warning("skipping malformed KB line")
        return entries

    def entries(self) -> list[KBEntry]:
        """All stored records, oldest first (read-only view for the dashboard)."""
        return self._load()

    def search(self, issue: PodIssue, query_text: str) -> Optional[KBEntry]:
        query_tokens = tokenize(f"{issue.symptom.value} {issue.message} {query_text}")
        if not query_tokens:
            return None

        best: Optional[KBEntry] = None
        best_score = 0.0
        for entry in self._load():
            if not entry.verified:
                continue
            if entry.symptom != issue.symptom.value:
                continue
            entry_tokens = tokenize(
                f"{entry.symptom} {entry.problem} {entry.root_cause} {entry.solution}"
            )
            score = _jaccard(query_tokens, entry_tokens)
            if score > best_score:
                best, best_score = entry, score

        if best and best_score >= self.score_threshold:
            log.info("KB hit for %s (score=%.2f)", issue.key, best_score)
            return best
        return None

    def add(self, entry: KBEntry) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        log.info("KB entry added for symptom=%s", entry.symptom)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
