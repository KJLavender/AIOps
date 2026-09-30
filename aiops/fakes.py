"""In-memory fake cluster so the pipeline can be demonstrated without kubectl.

Serves the four fault-simulation cases from the spec. `patch` marks the target
Deployment as healed, so the OOMKilled case runs the full loop end-to-end
(detect -> patch -> validate -> learn).
"""
from __future__ import annotations

from typing import Any, Optional

_DEPLOYMENTS = {
    "case1-crashloop": {"symptom": "CrashLoopBackOff", "memory": None},
    "case2-oomkilled": {"symptom": "OOMKilled", "memory": "64Mi"},
    "case3-imagepull": {"symptom": "ImagePullBackOff", "memory": None},
    "case4-missing-env": {"symptom": "CrashLoopBackOff", "memory": None},
}

_LOGS = {
    "case1-crashloop": "+ exit 1\n",
    "case2-oomkilled": "Killed\n",
    "case3-imagepull": "",
    "case4-missing-env": 'Traceback ...\nException: DB_HOST is not set\n',
}


class FakeKubeClient:
    def __init__(self) -> None:
        self.healed: set[str] = set()

    # --- reads -------------------------------------------------------------
    def list_pods(
        self, namespace: Optional[str] = None, selector: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if selector:
            name = selector.split("=", 1)[-1]
            return [self._pod(name)]
        return [self._pod(name) for name in _DEPLOYMENTS]

    def get(self, kind: str, name: str, namespace: str) -> dict[str, Any]:
        return self._deployment(name)

    def describe_pod(self, namespace: str, pod: str) -> str:
        return f"Name: {pod}\nNamespace: {namespace}\n"

    def pod_logs(self, namespace, pod, container=None, previous=False) -> str:
        return _LOGS.get(_dep_of(pod), "")

    def pod_events(self, namespace: str, pod: str) -> str:
        return f"Warning  {_DEPLOYMENTS.get(_dep_of(pod), {}).get('symptom', '')}  pod/{pod}\n"

    def get_owner_deployment(self, namespace: str, pod: str):
        name = _dep_of(pod)
        if name in _DEPLOYMENTS:
            return name, self._deployment(name)
        return None, None

    # --- writes ------------------------------------------------------------
    def patch(self, kind, name, namespace, patch, patch_type="strategic", dry_run=False):
        self.healed.add(name)
        return 0, f"deployment.apps/{name} patched (fake)", ""

    def rollout_status(self, namespace, deployment, timeout_seconds):
        return 0, f"deployment \"{deployment}\" successfully rolled out", ""

    # --- helpers -----------------------------------------------------------
    def _deployment(self, name: str) -> dict[str, Any]:
        spec = _DEPLOYMENTS.get(name, {})
        container: dict[str, Any] = {"name": "app"}
        if spec.get("memory"):
            container["resources"] = {"limits": {"memory": spec["memory"]}}
        return {
            "metadata": {"name": name},
            "spec": {
                "selector": {"matchLabels": {"app": name}},
                "template": {"spec": {"containers": [container]}},
            },
        }

    def _pod(self, dep_name: str) -> dict[str, Any]:
        pod_name = f"{dep_name}-abc123"
        if dep_name in self.healed:
            return _healthy_pod(dep_name, pod_name)
        return _faulty_pod(dep_name, pod_name, _DEPLOYMENTS[dep_name]["symptom"])


def _dep_of(pod: str) -> str:
    return pod.rsplit("-", 1)[0]


def _healthy_pod(dep_name: str, pod_name: str) -> dict[str, Any]:
    return {
        "metadata": {"namespace": "aiops-demo", "name": pod_name, "labels": {"app": dep_name}},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "app", "restartCount": 0, "state": {"running": {}}}],
        },
    }


def _faulty_pod(dep_name: str, pod_name: str, symptom: str) -> dict[str, Any]:
    meta = {"namespace": "aiops-demo", "name": pod_name, "labels": {"app": dep_name}}
    if symptom == "OOMKilled":
        cs = {
            "name": "app",
            "restartCount": 3,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {"terminated": {"reason": "OOMKilled", "message": "OOMKilled"}},
        }
        return {"metadata": meta, "status": {"phase": "Running", "containerStatuses": [cs]}}
    cs = {
        "name": "app",
        "restartCount": 5,
        "state": {"waiting": {"reason": symptom, "message": f"{symptom} for pod"}},
    }
    return {"metadata": meta, "status": {"phase": "Pending", "containerStatuses": [cs]}}
