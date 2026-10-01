import pytest

from aiops.actions import UnsafeAction, allowed_for, build_plan
from aiops.config import Config
from aiops.models import Symptom
from aiops.websearch import generalize, parse_mcp_response


def _dep(**container):
    return {"spec": {"template": {"spec": {"containers": [{"name": "app", **container}]}}}}


_PROBE = {"readinessProbe": {"httpGet": {"path": "/healthz", "port": 80}}}


def _container(plan):
    return plan.patch["spec"]["template"]["spec"]["containers"][0]


def test_probe_path_fix_keeps_port_via_strategic_merge():
    plan = build_plan("set_readiness_probe_path", {"path": "/"}, Symptom.NOT_READY,
                      _dep(**_PROBE), "app", Config())
    assert _container(plan) == {"name": "app", "readinessProbe": {"httpGet": {"path": "/"}}}


@pytest.mark.parametrize("path", ["", "healthz", "/$(rm -rf /)", "/a b", "/" + "x" * 200])
def test_probe_path_rejects_bad_paths(path):
    with pytest.raises(UnsafeAction):
        build_plan("set_readiness_probe_path", {"path": path}, Symptom.NOT_READY,
                   _dep(**_PROBE), "app", Config())


def test_action_must_match_symptom():
    # Web content could push "raise memory" for a readiness problem: not allowed.
    with pytest.raises(UnsafeAction):
        build_plan("set_memory_limit", {"memory": "1Gi"}, Symptom.NOT_READY,
                   _dep(**_PROBE), "app", Config())
    assert allowed_for(Symptom.CRASH_LOOP_BACKOFF) == ()


def test_unknown_action_is_rejected():
    with pytest.raises(UnsafeAction):
        build_plan("set_image", {"image": "evil/miner"}, Symptom.NOT_READY,
                   _dep(**_PROBE), "app", Config())


def test_memory_limit_bounded_and_increasing():
    dep = _dep(resources={"limits": {"memory": "512Mi"}})
    plan = build_plan("set_memory_limit", {"memory": "1Gi"}, Symptom.OOM_KILLED, dep, "app", Config())
    assert _container(plan)["resources"]["limits"]["memory"] == "1Gi"
    with pytest.raises(UnsafeAction):  # not an increase
        build_plan("set_memory_limit", {"memory": "256Mi"}, Symptom.OOM_KILLED, dep, "app", Config())
    with pytest.raises(UnsafeAction):  # above the 2Gi cap
        build_plan("set_memory_limit", {"memory": "64Gi"}, Symptom.OOM_KILLED, dep, "app", Config())


def test_lower_requests_only_decreases():
    dep = _dep(resources={"requests": {"memory": "256Gi", "cpu": "2"}})
    plan = build_plan("lower_requests", {"memory": "256Mi", "cpu": "500m"}, Symptom.PENDING,
                      dep, "app", Config())
    assert _container(plan)["resources"]["requests"] == {"memory": "256Mi", "cpu": "500m"}
    with pytest.raises(UnsafeAction):
        build_plan("lower_requests", {"memory": "512Gi"}, Symptom.PENDING, dep, "app", Config())


def test_restart_patches_template_annotation():
    plan = build_plan("rollout_restart", {}, Symptom.NOT_READY, _dep(), "app", Config())
    annotations = plan.patch["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in annotations


def test_parse_mcp_sse_and_json():
    sse = 'event: message\ndata: {"result":{"content":[{"type":"text","text":"Title: fix"}]}}\n'
    assert parse_mcp_response(sse) == "Title: fix"
    body = '{"result":{"content":[{"type":"text","text":"a"},{"type":"text","text":"b"}]}}'
    assert parse_mcp_response(body) == "a\n\nb"
    assert parse_mcp_response("not json") == ""


def test_generalize_strips_ips_and_pod_hashes():
    text = 'Readiness probe failed: Get "http://10.42.0.31:80/healthz" pod case6-readiness-796789b4fd-w7jzg'
    out = generalize(text)
    assert "10.42" not in out and "796789b4fd" not in out
    assert "/healthz" in out
