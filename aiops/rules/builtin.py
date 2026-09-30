"""Built-in rules for the known symptoms in the spec."""
from __future__ import annotations

import logging
from typing import Optional

from ..models import Diagnosis, DiagnosisSource, PodIssue, Symptom
from ..patches import memory_limit_patch
from ..util import format_memory_mib, parse_memory_to_bytes
from .base import Rule, RuleContext

log = logging.getLogger("aiops.rules")


class OOMKilledRule(Rule):
    """Rule 1: OOMKilled -> raise the memory limit and let the Deployment restart."""

    symptom = Symptom.OOM_KILLED
    name = "oom-killed"

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        dep_name, dep_obj = ctx.owner_deployment(issue)
        new_memory = self._compute_memory(issue, ctx, dep_obj)

        actions = ["Increase Memory Limit", "Restart Deployment"]
        if not dep_name or not dep_obj:
            # No owning Deployment (bare pod) -> we can only advise.
            return Diagnosis(
                source=DiagnosisSource.RULE,
                symptom=self.symptom,
                root_cause="memory limit too low (container OOMKilled)",
                summary="Pod OOMKilled but has no owning Deployment to patch.",
                actions=actions,
                auto_fixable=False,
            )

        from ..kube import container_from_deployment

        container = container_from_deployment(dep_obj, issue.container)
        cname = container.get("name") if container else issue.container
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=self.symptom,
            root_cause="memory limit too low (container OOMKilled)",
            summary=f"Raise memory limit to {new_memory} on {dep_name}/{cname}.",
            actions=actions,
            patch=memory_limit_patch(cname, new_memory),
            target_kind="deployment",
            target_name=dep_name,
            target_namespace=issue.namespace,
            auto_fixable=True,
            confidence=0.9,
        )

    def _compute_memory(self, issue: PodIssue, ctx: RuleContext, dep_obj) -> str:
        default = ctx.config.default_memory_limit
        if not dep_obj:
            return default
        from ..kube import container_from_deployment

        container = container_from_deployment(dep_obj, issue.container)
        current = (
            (container or {})
            .get("resources", {})
            .get("limits", {})
            .get("memory")
        )
        if not current:
            return default
        try:
            scaled = int(parse_memory_to_bytes(current) * ctx.config.memory_scale_factor)
            default_bytes = parse_memory_to_bytes(default)
            return format_memory_mib(max(scaled, default_bytes))
        except (ValueError, TypeError):
            return default


class ImagePullRule(Rule):
    """Rule 2: ImagePullBackOff / ErrImagePull -> check tag & registry access.

    We do NOT guess a 'correct' image automatically; that needs KB/human input.
    """

    symptom = Symptom.IMAGE_PULL_BACKOFF
    name = "image-pull"

    def matches(self, issue: PodIssue) -> bool:
        return issue.symptom in (Symptom.IMAGE_PULL_BACKOFF, Symptom.ERR_IMAGE_PULL)

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=issue.symptom,
            root_cause="image cannot be pulled (bad tag or registry access)",
            summary=f"Verify image tag and registry access: {issue.message}",
            actions=["Check Image Tag", "Check Registry Access"],
            auto_fixable=False,
            forward_to_kb=True,  # a KB entry may know the correct image
        )


class CrashLoopBackOffRule(Rule):
    """Rule 3: CrashLoopBackOff -> collect logs/events and hand off to KB/LLM."""

    symptom = Symptom.CRASH_LOOP_BACKOFF
    name = "crash-loop"

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=self.symptom,
            root_cause="application crashes on start (see logs)",
            summary="Collected logs & events; forwarding to Knowledge Base / LLM.",
            actions=["Collect Logs", "Collect Events", "Forward to Knowledge Base"],
            auto_fixable=False,
            forward_to_kb=True,
        )
