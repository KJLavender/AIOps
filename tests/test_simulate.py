import json

from aiops import simulate
from aiops.config import Config
from aiops.judge import Judge
from aiops.models import Diagnosis, DiagnosisSource


def _scenarios():
    return {s["name"]: s for s in json.loads(simulate.SCENARIOS.read_text(encoding="utf-8"))}


def _stub_llm(monkeypatch, path):
    class _LLM:
        def __init__(self, config):
            pass

        def analyze(self, issue, data):
            return Diagnosis(DiagnosisSource.LLM, issue.symptom, "probe path 404", "use another path",
                             confidence=0.9, proposed_action="set_readiness_probe_path",
                             action_params={"path": path})

    monkeypatch.setattr(simulate, "OllamaAnalyzer", _LLM)
    monkeypatch.setattr(Judge, "_ask", lambda self, prompt: {"grounded": 1, "fits": 1, "safe": 1})


def test_good_fix_passes(monkeypatch):
    _stub_llm(monkeypatch, "/")
    r = simulate.run_scenario(_scenarios()["nginx-healthz-404"], Config(), "default")
    assert r.passed and r.applied_action == "set_readiness_probe_path" and not r.unsafe


def test_poisoned_results_are_blocked(monkeypatch):
    _stub_llm(monkeypatch, "/")
    r = simulate.run_scenario(_scenarios()["poisoned-search-result"], Config(), "default")
    assert r.passed and len(r.trace["guard_flags"]) >= 1


def test_guessed_path_is_caught_by_precheck(monkeypatch):
    _stub_llm(monkeypatch, "/healthz")  # nginx doesn't serve it
    r = simulate.run_scenario(_scenarios()["misleading-memory-advice"], Config(), "default")
    assert r.applied_action is None and r.trace["precheck"].startswith("new path /healthz answered 404")


def test_forbidden_change_fails_the_scenario(monkeypatch):
    _stub_llm(monkeypatch, "/ready-now")
    sc = dict(_scenarios()["slow-start-timeout"], serves=["/ready", "/ready-now"])
    r = simulate.run_scenario(sc, Config(), "default")
    assert r.unsafe and not r.passed


def test_summary_prefers_safe_then_accurate():
    R = simulate.Result
    s = simulate.summarize([R("a", "v1", True, None, False), R("b", "v1", False, "x", True),
                            R("a", "v2", True, None, False), R("b", "v2", False, None, False)])
    assert s["best"] == "v2" and s["variants"]["v1"]["unsafe"] == 1
