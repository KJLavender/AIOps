from aiops.config import Config
from aiops.kube import container_from_deployment
from aiops.models import PodIssue, Symptom
from aiops.rules import RuleEngine
from aiops.rules.base import RuleContext


class _FakeKube:
    def __init__(self, deployment=None):
        self._deployment = deployment

    def get_owner_deployment(self, namespace, pod):
        if self._deployment is None:
            return None, None
        return "case2-oomkilled", self._deployment


def _deployment(memory="64Mi"):
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "resources": {"limits": {"memory": memory}},
                        }
                    ]
                }
            }
        }
    }


def _ctx(kube):
    return RuleContext(kube=kube, config=Config())


def _limit_memory(diag):
    return diag.patch["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]


def test_oom_rule_applies_memory_floor():
    # 64Mi doubled is 128Mi, but the 512Mi default floor wins (matches the spec).
    kube = _FakeKube(_deployment("64Mi"))
    issue = PodIssue("aiops-demo", "case2-oomkilled-x", "app", Symptom.OOM_KILLED)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.auto_fixable is True
    assert diag.target_kind == "deployment"
    assert _limit_memory(diag) == "512Mi"


def test_oom_rule_scales_when_above_floor():
    # 512Mi doubled = 1Gi, above the floor, so scaling wins.
    kube = _FakeKube(_deployment("512Mi"))
    issue = PodIssue("aiops-demo", "big", "app", Symptom.OOM_KILLED)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert _limit_memory(diag) == "1Gi"


def test_oom_rule_without_deployment_is_advisory():
    kube = _FakeKube(None)
    issue = PodIssue("aiops-demo", "bare-pod", "app", Symptom.OOM_KILLED)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.auto_fixable is False


def test_imagepull_rule_is_advisory_and_forwards():
    kube = _FakeKube(None)
    issue = PodIssue("aiops-demo", "case3", "app", Symptom.IMAGE_PULL_BACKOFF, "not found")
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.auto_fixable is False
    assert diag.forward_to_kb is True


def test_crashloop_rule_forwards_to_kb():
    kube = _FakeKube(None)
    issue = PodIssue("aiops-demo", "case1", "app", Symptom.CRASH_LOOP_BACKOFF)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.forward_to_kb is True
    assert diag.auto_fixable is False


def test_container_from_deployment_by_name():
    dep = _deployment()
    assert container_from_deployment(dep, "app")["name"] == "app"
    assert container_from_deployment(dep, None)["name"] == "app"
