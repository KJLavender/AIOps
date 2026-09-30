import os
import tempfile

from aiops.knowledge_base import JsonlKnowledgeBase
from aiops.models import KBEntry, PodIssue, Symptom


def _kb():
    tmp = tempfile.mkdtemp()
    return JsonlKnowledgeBase(os.path.join(tmp, "kb.jsonl"), score_threshold=0.2)


def test_add_and_search_roundtrip():
    kb = _kb()
    kb.add(
        KBEntry(
            symptom="OOMKilled",
            problem="container OOMKilled memory",
            root_cause="memory limit too low",
            solution="increase memory to 512Mi",
            verified=True,
        )
    )
    issue = PodIssue("ns", "pod", "app", Symptom.OOM_KILLED, "container OOMKilled memory")
    hit = kb.search(issue, "")
    assert hit is not None
    assert hit.solution == "increase memory to 512Mi"


def test_unverified_entries_ignored():
    kb = _kb()
    kb.add(
        KBEntry(
            symptom="OOMKilled",
            problem="oom",
            root_cause="x",
            solution="y",
            verified=False,
        )
    )
    issue = PodIssue("ns", "pod", "app", Symptom.OOM_KILLED, "oom")
    assert kb.search(issue, "") is None


def test_symptom_mismatch_returns_none():
    kb = _kb()
    kb.add(
        KBEntry(symptom="OOMKilled", problem="oom", root_cause="x", solution="y", verified=True)
    )
    issue = PodIssue("ns", "pod", "app", Symptom.CRASH_LOOP_BACKOFF, "crash")
    assert kb.search(issue, "") is None
