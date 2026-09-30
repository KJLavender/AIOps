from aiops.collector import Collector
from aiops.config import Config
from aiops.models import Symptom


class _FakeKube:
    def __init__(self, pods):
        self._pods = pods

    def list_pods(self, namespace=None, selector=None):
        return self._pods


def _analyze(pod):
    collector = Collector(_FakeKube([pod]), Config(all_namespaces=True))
    return collector.scan()


def test_detects_crashloop():
    pod = {
        "metadata": {"namespace": "aiops-demo", "name": "case1-abc"},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 5,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                }
            ],
        },
    }
    issues = _analyze(pod)
    assert len(issues) == 1
    assert issues[0].symptom == Symptom.CRASH_LOOP_BACKOFF
    assert issues[0].container == "app"


def test_detects_oomkilled_from_last_state():
    pod = {
        "metadata": {"namespace": "aiops-demo", "name": "case2-abc"},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 3,
                    "state": {"running": {}},
                    "lastState": {"terminated": {"reason": "OOMKilled"}},
                }
            ],
        },
    }
    issues = _analyze(pod)
    assert issues[0].symptom == Symptom.OOM_KILLED


def test_detects_imagepull():
    pod = {
        "metadata": {"namespace": "aiops-demo", "name": "case3-abc"},
        "status": {
            "phase": "Pending",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "ImagePullBackOff", "message": "not found"}},
                }
            ],
        },
    }
    issues = _analyze(pod)
    assert issues[0].symptom == Symptom.IMAGE_PULL_BACKOFF


def test_healthy_pod_no_issue():
    pod = {
        "metadata": {"namespace": "default", "name": "ok"},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "app", "restartCount": 0, "state": {"running": {}}}],
        },
    }
    assert _analyze(pod) == []
