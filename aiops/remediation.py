"""Applies structured, auto-fixable patches via kubectl."""
from __future__ import annotations

import logging

from .config import Config
from .kube import KubeClient
from .models import Diagnosis, RemediationResult

log = logging.getLogger("aiops.remediation")


class Remediator:
    def __init__(self, kube: KubeClient, config: Config) -> None:
        self.kube = kube
        self.config = config

    def remediate(self, diagnosis: Diagnosis) -> RemediationResult:
        if not (diagnosis.auto_fixable and diagnosis.patch):
            return RemediationResult(
                attempted=False, success=False, detail="no auto-applicable patch"
            )
        if not (diagnosis.target_kind and diagnosis.target_name and diagnosis.target_namespace):
            return RemediationResult(
                attempted=False, success=False, detail="patch target missing"
            )

        if self.config.dry_run:
            log.info(
                "[dry-run] would patch %s/%s in %s: %s",
                diagnosis.target_kind,
                diagnosis.target_name,
                diagnosis.target_namespace,
                diagnosis.patch,
            )
            return RemediationResult(
                attempted=True, success=True, detail="dry-run", patch=diagnosis.patch
            )

        rc, out, err = self.kube.patch(
            diagnosis.target_kind,
            diagnosis.target_name,
            diagnosis.target_namespace,
            diagnosis.patch,
        )
        if rc == 0:
            log.info(
                "patched %s/%s: %s",
                diagnosis.target_kind,
                diagnosis.target_name,
                out.strip(),
            )
            return RemediationResult(
                attempted=True, success=True, detail=out.strip(), patch=diagnosis.patch
            )
        return RemediationResult(
            attempted=True, success=False, detail=(err or out).strip(),
            patch=diagnosis.patch,
        )

    def rollback(self, diagnosis: Diagnosis) -> RemediationResult:
        """Undo our patch (previous ReplicaSet template) after a failed validation."""
        if self.config.dry_run or not (diagnosis.target_name and diagnosis.target_namespace):
            return RemediationResult(attempted=False, success=False, detail="nothing to roll back")
        rc, out, err = self.kube.rollout_undo(diagnosis.target_namespace, diagnosis.target_name)
        detail = (out if rc == 0 else err or out).strip()
        log.info("rollback %s/%s: %s", diagnosis.target_namespace, diagnosis.target_name, detail)
        return RemediationResult(attempted=True, success=rc == 0, detail=detail)
