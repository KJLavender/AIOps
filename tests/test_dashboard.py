import json
import os
import tempfile
import urllib.request

from aiops.app import Agent
from aiops.config import Config
from aiops.dashboard import Dashboard
from aiops.events import EventLog, workload_of
from aiops.fakes import FakeKubeClient
from aiops.models import PodIssue, Symptom


def _demo_agent():
    config = Config(
        kb_path=os.path.join(tempfile.mkdtemp(), "kb.jsonl"),
        validation_stability_seconds=0,
        validation_poll_seconds=0,
    )
    return Agent(config, kube=FakeKubeClient())


def _by_workload(state):
    return {p["workload"]: p for p in state["pods"]}


def test_workload_of_strips_replicaset_hash():
    pod = {"metadata": {"name": "web-7c9f5d-abc12",
                        "ownerReferences": [{"kind": "ReplicaSet", "name": "web-7c9f5d"}]}}
    assert workload_of(pod) == "web"
    assert workload_of({"metadata": {"name": "bare"}}) == "bare"


def test_event_log_newest_first_and_bounded():
    events = EventLog(maxlen=3)
    issue = PodIssue("ns", "web-7c9f5d-abc12", "app", Symptom.OOM_KILLED)
    for i in range(5):
        events.emit("detected", f"e{i}", issue)
    recent = events.recent()
    assert [e["detail"] for e in recent] == ["e4", "e3", "e2"]
    assert recent[0]["symptom"] == "OOMKilled"


def test_state_before_scan_shows_all_demo_pods_broken():
    agent = _demo_agent()
    state = Dashboard(agent, "127.0.0.1", 0).state()
    pods = _by_workload(state)
    assert len(pods) == 4
    assert pods["case2-oomkilled"]["state"] == "OOMKilled"
    assert pods["case2-oomkilled"]["level"] == "bad"
    assert pods["case2-oomkilled"]["containers"][0]["memory_limit"] == "64Mi"
    assert state["events"] == []
    assert state["agent"]["demo"] is True


def test_state_after_scan_shows_fix_and_timeline():
    agent = _demo_agent()
    agent.tick()
    dash = Dashboard(agent, "127.0.0.1", 0)
    state = dash.state()
    pods = _by_workload(state)

    oom = pods["case2-oomkilled"]
    assert oom["level"] == "ok"
    assert oom["name"] == "case2-oomkilled-def456"  # replaced pod, same workload
    assert oom["containers"][0]["memory_limit"] == "512Mi"
    assert pods["case3-imagepull"]["level"] == "bad"

    stages = [e["stage"] for e in state["events"] if e["workload"] == "case2-oomkilled"]
    assert stages == ["learned", "validated", "remediated", "diagnosed", "detected"]
    assert state["kb"]["total"] == 1
    assert state["agent"]["last_scan_at"] is not None


def test_http_endpoints_serve_page_and_state():
    agent = _demo_agent()
    dash = Dashboard(agent, "127.0.0.1", 0)
    dash.start()
    try:
        base = f"http://127.0.0.1:{dash.port}"
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            assert "AIOps Dashboard" in resp.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/state", timeout=5) as resp:
            assert len(json.loads(resp.read())["pods"]) == 4
    finally:
        dash.stop()


def test_metrics_endpoint_counts_events():
    agent = _demo_agent()
    agent.tick()
    text = Dashboard(agent, "127.0.0.1", 0).metrics()
    assert 'aiops_events_total{stage="detected",source="",namespace="aiops-demo"}' in text
    assert "aiops_kb_entries " in text
    assert "aiops_scan_ok 1" in text
