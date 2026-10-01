"""In-memory timeline of what the agent did (feeds the dashboard)."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .models import PodIssue


@dataclass
class Event:
    ts: str
    stage: str  # detected / diagnosed / recommended / remediated / validated / learned / ...
    level: str  # info / ok / warn / error
    detail: str
    namespace: str = ""
    pod: str = ""
    workload: str = ""
    symptom: str = ""
    source: str = ""  # rule / kb / llm


def workload_of(pod: dict[str, Any]) -> str:
    """Best-effort owning workload name (Pod -> ReplicaSet -> Deployment by naming)."""
    meta = pod.get("metadata", {})
    for owner in meta.get("ownerReferences", []):
        if owner.get("kind") == "ReplicaSet":
            return owner.get("name", "").rsplit("-", 1)[0]
        if owner.get("name"):
            return owner["name"]
    return meta.get("name", "")


class EventLog:
    """Thread-safe ring buffer; the agent loop writes, the dashboard reads."""

    def __init__(self, maxlen: int = 500) -> None:
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        # Monotonic per-(stage, source, namespace) totals for Prometheus.
        self._counts: dict[tuple[str, str, str], int] = {}

    def emit(
        self,
        stage: str,
        detail: str,
        issue: Optional[PodIssue] = None,
        level: str = "info",
        source: str = "",
    ) -> None:
        event = Event(
            ts=datetime.now(timezone.utc).isoformat(),
            stage=stage,
            level=level,
            detail=detail,
            source=source,
        )
        if issue is not None:
            event.namespace = issue.namespace
            event.pod = issue.pod
            event.workload = workload_of(issue.raw) if issue.raw else issue.pod
            event.symptom = issue.symptom.value
        with self._lock:
            self._events.append(event)
            key = (stage, source, event.namespace)
            self._counts[key] = self._counts.get(key, 0) + 1

    def counts(self) -> dict[tuple[str, str, str], int]:
        with self._lock:
            return dict(self._counts)

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            events = list(self._events)[-limit:]
        return [asdict(e) for e in reversed(events)]
