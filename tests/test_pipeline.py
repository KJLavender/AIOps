import json
import os
import tempfile

from aiops.config import Config
from aiops.knowledge_base import JsonlKnowledgeBase
from aiops.learning import LearningEngine
from aiops.llm import LLMAnalyzer
from aiops.llm.base import LLMInput
from aiops.llm.ollama import OllamaAnalyzer
from aiops.models import Diagnosis, DiagnosisSource, KBEntry, PodIssue, Symptom
from aiops.pipeline import Pipeline
from aiops.remediation import Remediator
from aiops.rules import RuleEngine
from aiops.validation import Validator


class _Kube:
    """Minimal kube: one Deployment named `dep_name`; records patches."""

    def __init__(self, dep_name, logs=""):
        self.dep_name = dep_name
        self.logs = logs
        self.patches = []

    def get_owner_deployment(self, namespace, pod):
        dep = {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "x:1"}]}}}}
        return self.dep_name, dep

    def pod_logs(self, *args, **kwargs):
        return self.logs

    def pod_events(self, *args):
        return ""

    def describe_pod(self, *args):
        return ""

    def patch(self, kind, name, namespace, patch, **kwargs):
        self.patches.append((name, patch))
        return 0, "patched", ""


class _SpyLLM(LLMAnalyzer):
    def __init__(self):
        self.calls = 0

    def analyze(self, issue, data):
        self.calls += 1
        return Diagnosis(DiagnosisSource.LLM, issue.symptom, "llm guess", "llm fix")


def _pipeline(kube, kb_entries=(), llm=None, **config):
    config = Config(
        kb_path=os.path.join(tempfile.mkdtemp(), "kb.jsonl"),
        llm_enabled=True,
        dry_run=True,
        **config,
    )
    kb = JsonlKnowledgeBase(config.kb_path, score_threshold=0.1)
    for entry in kb_entries:
        kb.add(entry)
    pipeline = Pipeline(
        kube=kube, config=config, rule_engine=RuleEngine(), kb=kb,
        llm=llm or _SpyLLM(), remediator=Remediator(kube, config),
        validator=Validator(kube, config), learning=LearningEngine(kb, config),
    )
    return pipeline


def _image_entry(target_name):
    return KBEntry(
        symptom="ErrImagePull",
        problem="failed to pull image not found",
        root_cause="image tag does not exist",
        solution="switch to nginx:1.27-alpine",
        verified=True,
        patch={"spec": {"template": {"spec": {"containers": [
            {"name": "app", "image": "nginx:1.27-alpine"}]}}}},
        target_kind="deployment",
        target_name=target_name,
    )


def _last_event(pipeline):
    return pipeline.events.recent()[0]


def test_kb_patch_from_other_workload_is_not_auto_applied():
    kube = _Kube("other-app")
    pipeline = _pipeline(kube, [_image_entry("case3-imagepull")])
    issue = PodIssue("ns", "other-app-x", "app", Symptom.ERR_IMAGE_PULL,
                     "failed to pull image not found")
    pipeline.handle(issue)
    assert _last_event(pipeline)["stage"] == "recommended"


def test_kb_patch_for_same_workload_is_auto_applied():
    kube = _Kube("case3-imagepull")
    pipeline = _pipeline(kube, [_image_entry("case3-imagepull")])
    issue = PodIssue("ns", "case3-imagepull-x", "app", Symptom.ERR_IMAGE_PULL,
                     "failed to pull image not found")
    diag = pipeline._search_kb_or_llm(issue, pipeline._build_context(issue))
    assert diag.source == DiagnosisSource.KB
    assert diag.auto_fixable is True


def test_precise_rule_skips_llm():
    kube = _Kube("case4-missing-env", logs="Exception: DB_HOST is not set")
    llm = _SpyLLM()
    pipeline = _pipeline(kube, llm=llm)
    issue = PodIssue("ns", "case4-x", "app", Symptom.CRASH_LOOP_BACKOFF, "Error: exit 1")
    pipeline.handle(issue)
    assert llm.calls == 0
    assert "DB_HOST" in _last_event(pipeline)["detail"]


def test_generic_crash_still_asks_llm():
    kube = _Kube("case1-crashloop", logs="+ exit 1")
    llm = _SpyLLM()
    pipeline = _pipeline(kube, llm=llm)
    pipeline.handle(PodIssue("ns", "case1-x", "app", Symptom.CRASH_LOOP_BACKOFF))
    assert llm.calls == 1


def test_learned_entry_remembers_target():
    kube = _Kube("case4-missing-env", logs="Exception: DB_HOST is not set")
    pipeline = _pipeline(kube, env_defaults={"DB_HOST": "db.local"})
    pipeline.validator.validate = lambda d: type("V", (), {"success": True, "detail": "ok"})()
    pipeline.handle(PodIssue("ns", "case4-x", "app", Symptom.CRASH_LOOP_BACKOFF))
    entries = pipeline.kb.entries()
    assert entries[-1].target_name == "case4-missing-env"


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps({"response": json.dumps(body)}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ollama_answer(monkeypatch, body, **config):
    monkeypatch.setattr(
        "aiops.llm.ollama.urllib.request.urlopen", lambda req, timeout: _FakeResponse(body)
    )
    analyzer = OllamaAnalyzer(Config(**config))
    issue = PodIssue("ns", "p", "app", Symptom.CRASH_LOOP_BACKOFF)
    return analyzer.analyze(issue, LLMInput())


def test_llm_low_confidence_is_discarded(monkeypatch):
    body = {"root_cause": "PodIPFilter", "fix": "Use pod network", "confidence": 0.3}
    assert _ollama_answer(monkeypatch, body) is None


def test_llm_percentage_confidence_is_normalised(monkeypatch):
    body = {"root_cause": "bad tag", "fix": "use nginx:alpine", "confidence": 85}
    diag = _ollama_answer(monkeypatch, body)
    assert diag.confidence == 0.85
    assert diag.auto_fixable is False


def test_llm_threshold_is_configurable(monkeypatch):
    body = {"root_cause": "x", "fix": "y", "confidence": 0.3}
    assert _ollama_answer(monkeypatch, body, llm_min_confidence=0.2) is not None
