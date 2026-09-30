"""Knowledge Base interface.

Phase 1 uses keyword scoring (JsonlKnowledgeBase). The interface is kept
minimal so a vector-search backend (Qdrant / Chroma / Elasticsearch / Milvus)
can be dropped in later without touching the pipeline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import KBEntry, PodIssue


class KnowledgeBase(ABC):
    @abstractmethod
    def search(self, issue: PodIssue, query_text: str) -> Optional[KBEntry]:
        """Return the best verified entry for this issue, or None."""

    @abstractmethod
    def add(self, entry: KBEntry) -> None:
        """Persist a verified entry."""
