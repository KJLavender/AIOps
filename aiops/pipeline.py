"""The MVP pipeline:

  Pod fails -> Rule Engine -> collect Logs -> KB Search -> LLM -> fix
            -> Auto Remediation -> Validation -> Learn
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from . import actions
from .collector import Collector
from .config import Config
from .events import EventLog
from .kube import KubeClient, container_from_deployment
from .knowledge_base import KnowledgeBase
from .learning import LearningEngine
from .llm import LLMAnalyzer, LLMInput
from .models import Diagnosis, DiagnosisSource, KBEntry, PodIssue
from .remediation import Remediator
from .rules import RuleEngine
from .rules.base import RuleContext
from .validation import Validator
from .websearch import WebSearch, generalize
from . import guard
from .judge import Judge
import time

log = logging.getLogger("aiops.pipeline")


class Pipeline:
    def __init__(
        self,
        kube: KubeClient,
        config: Config,
        rule_engine: RuleEngine,
        kb: KnowledgeBase,
        llm: LLMAnalyzer,
        remediator: Remediator,
        validator: Validator,
        learning: LearningEngine,
        events: Optional[EventLog] = None,
        websearch: Optional[WebSearch] = None,
    ) -> None:
        self.kube = kube
        self.config = config
        self.rule_engine = rule_engine
        self.kb = kb
        self.llm = llm
        self.remediator = remediator
        self.validator = validator
        self.learning = learning
        self.events = events if events is not None else EventLog()
        self.websearch = websearch if websearch is not None else WebSearch(config)
        self.judge = Judge(config)
        # Decision record for the issue being handled (Monitor): filled by the
        # helpers, written as one JSON log line when handling ends.
        self._trace: dict = {}
        # Fixes that failed validation (and were rolled back): never retried.
        self._failed_fixes: set[tuple[str, str, str]] = set()

    def handle(self, issue: PodIssue) -> None:
        self._trace = {}
        try:
            self._handle(issue)
        finally:
            if "llm" in self._trace:
                self._trace.update(issue=issue.key, symptom=issue.symptom.value)
                log.info("decision %s", json.dumps(self._trace, ensure_ascii=False, default=str))

    def _handle(self, issue: PodIssue) -> None:
        log.info("handling issue %s: %s", issue.key, issue.message.strip()[:120])
        self.events.emit("detected", issue.message.strip()[:200], issue, level="warn")
        ctx = self._build_context(issue)

        diagnosis = self.rule_engine.diagnose(issue, ctx)

        # KB / LLM fallback when there's no rule, or the rule defers to them.
        if diagnosis is None or (
            not diagnosis.auto_fixable and diagnosis.forward_to_kb
        ):
            allow_llm = diagnosis is None or diagnosis.consult_llm
            fallback = self._search_kb_or_llm(issue, ctx, allow_llm)
            # A precise rule answer is only replaced by a fix we can apply.
            if fallback is not None and (allow_llm or fallback.auto_fixable):
                diagnosis = fallback

        if diagnosis is None:
            log.warning("no diagnosis for %s; leaving for human review", issue.key)
            self.events.emit(
                "no_diagnosis", "No rule/KB/LLM match; needs human review", issue, level="error"
            )
            return

        self._report(issue, diagnosis)
        source = diagnosis.source.value
        self.events.emit(
            "diagnosed", f"root cause: {diagnosis.root_cause}", issue, source=source
        )

        if not (diagnosis.auto_fixable and diagnosis.patch):
            self._trace["outcome"] = "recommended"
            log.info("[recommendation only] %s -> %s", issue.key, diagnosis.summary)
            self.events.emit("recommended", diagnosis.summary, issue, level="warn", source=source)
            return

        if not self.config.auto_fix:
            self._trace["outcome"] = "auto_fix_disabled"
            log.info("auto_fix disabled; skipping remediation for %s", issue.key)
            self.events.emit(
                "recommended", f"auto-fix disabled: {diagnosis.summary}", issue,
                level="warn", source=source,
            )
            return

        remediation = self.remediator.remediate(diagnosis)
        if not remediation.success:
            self._trace["outcome"] = "remediation_failed"
            log.error("remediation failed for %s: %s", issue.key, remediation.detail)
            self.events.emit(
                "remediation_failed", remediation.detail, issue, level="error", source=source
            )
            return
        self.events.emit(
            "remediated", f"{diagnosis.summary} ({remediation.detail})", issue, source=source
        )

        validation = self.validator.validate(diagnosis)
        if validation.success:
            self._trace["outcome"] = "validated"
            self.learning.record(issue, diagnosis, remediation, validation)
            log.info("RESOLVED %s (%s)", issue.key, validation.detail)
            self.events.emit("validated", validation.detail, issue, level="ok", source=source)
            self.events.emit("learned", "verified fix stored in KB", issue, level="ok", source=source)
        else:
            self._trace["outcome"] = "validation_failed"
            log.error("validation failed for %s: %s", issue.key, validation.detail)
            self.events.emit(
                "validation_failed", validation.detail, issue, level="error", source=source
            )
            self._failed_fixes.add(_fix_key(diagnosis))
            if self.config.rollback_on_failure:
                undo = self.remediator.rollback(diagnosis)
                if undo.attempted:
                    self.events.emit(
                        "rolled_back" if undo.success else "rollback_failed", undo.detail,
                        issue, level="warn" if undo.success else "error", source=source,
                    )

    # --- helpers -----------------------------------------------------------
    def _build_context(self, issue: PodIssue) -> RuleContext:
        ctx = RuleContext(kube=self.kube, config=self.config)
        # Only pull logs/events for symptoms that need them (avoids noise/cost).
        needs_logs = issue.symptom.value in {"CrashLoopBackOff", "Failed", "NotReady"}
        if needs_logs:
            ctx.logs = self.kube.pod_logs(issue.namespace, issue.pod, issue.container)
            ctx.previous_logs = self.kube.pod_logs(
                issue.namespace, issue.pod, issue.container, previous=True
            )
            ctx.events = self.kube.pod_events(issue.namespace, issue.pod)
            ctx.describe = self.kube.describe_pod(issue.namespace, issue.pod)
        return ctx

    def _search_kb_or_llm(
        self, issue: PodIssue, ctx: RuleContext, allow_llm: bool = True
    ) -> Optional[Diagnosis]:
        # Same workload, same symptom, a fix that was verified before: reuse it
        # (keyword scoring drowns in log tokens, so check this first).
        dep_name, _ = ctx.owner_deployment(issue)
        exact = self.kb.find_for_target(issue.symptom.value, dep_name) if dep_name else None
        if exact is not None:
            log.info("KB exact hit for %s (workload %s)", issue.key, dep_name)
            return self._diagnosis_from_kb(issue, exact)
        query_text = f"{ctx.logs}\n{ctx.events}"
        hit = self.kb.search(issue, query_text)
        if hit is not None:
            return self._diagnosis_from_kb(issue, hit)

        if not (self.config.llm_enabled and allow_llm):
            return None

        dep_name, dep_obj = ctx.owner_deployment(issue)
        web_results = ""
        if self.config.web_search_enabled:
            query = self._search_query(issue, ctx)
            web_results = self.websearch.search(query)
            self.events.emit(
                "web_search",
                f"{query} -> {'found results' if web_results else 'no results'}",
                issue, source="web",
            )
            screened = guard.scan(web_results)
            if screened.flags:
                self.events.emit("guard_blocked", "; ".join(screened.flags)[:300], issue,
                                 level="warn", source="web")
            web_results = screened.text
            self._trace.update(web_query=query, web_chars=len(web_results),
                               guard_flags=screened.flags)
        data = LLMInput(
            logs=ctx.logs,
            previous_logs=ctx.previous_logs,
            events=ctx.events,
            describe=ctx.describe,
            deployment_yaml=json.dumps(dep_obj.get("spec", {}), ensure_ascii=False) if dep_obj else "",
            web_results=web_results,
            allowed_actions=actions.allowed_for(issue.symptom) if self.config.llm_auto_fix else (),
        )
        started = time.time()
        diagnosis = self.llm.analyze(issue, data)
        self._trace["llm"] = None if diagnosis is None else {
            "root_cause": diagnosis.root_cause, "action": diagnosis.proposed_action,
            "params": diagnosis.action_params, "confidence": diagnosis.confidence,
            "seconds": round(time.time() - started, 1),
        }
        if diagnosis is not None and diagnosis.proposed_action:
            self._promote_action(issue, diagnosis, dep_name, dep_obj, bool(web_results), ctx)
        return diagnosis

    def _search_query(self, issue: PodIssue, ctx: RuleContext) -> str:
        """Symptom + the most specific error line we have, minus cluster noise."""
        detail = ""
        for source in (ctx.events, ctx.previous_logs, ctx.logs):
            lines = [l for l in (source or "").splitlines() if l.strip()]
            hits = [l for l in lines if re.search(r"fail|error|exception|refused|denied", l, re.I)]
            if hits:
                detail = hits[-1]
                break
        if not detail:
            detail = issue.message
        # Events lines start with "LAST SEEN TYPE REASON OBJECT": keep the message.
        detail = re.sub(r"^\S+\s+(Warning|Normal)\s+\S+\s+\S+\s+", "", detail.strip())
        return generalize(f"kubernetes {issue.symptom.value} {detail}")[:200]

    def _promote_action(self, issue: PodIssue, diagnosis: Diagnosis,
                        dep_name: Optional[str], dep_obj, used_web: bool,
                        ctx: Optional[RuleContext] = None) -> None:
        """Turn an LLM-named catalog action into an auto-fix, if every guard passes."""
        name = diagnosis.proposed_action
        if not self.config.llm_auto_fix:
            return
        if diagnosis.confidence < self.config.llm_auto_fix_min_confidence:
            diagnosis.summary += f" (action {name} not applied: confidence {diagnosis.confidence:.2f})"
            return
        try:
            plan = actions.build_plan(name, diagnosis.action_params, issue.symptom,
                                      dep_obj, issue.container, self.config)
        except actions.UnsafeAction as exc:
            log.info("rejected LLM action for %s: %s", issue.key, exc)
            self.events.emit("action_rejected", f"{name}: {exc}", issue, level="warn", source="llm")
            return
        if self.config.judge_enabled:
            container = container_from_deployment(dep_obj, issue.container) or {}
            verdict = self.judge.review(
                issue, diagnosis, plan.summary,
                events=ctx.events if ctx else "", logs=(ctx.previous_logs or ctx.logs) if ctx else "",
                spec=json.dumps(container, ensure_ascii=False))
            self._trace["judge"] = verdict.to_dict()
            if not verdict.passed:
                log.info("judge rejected %s for %s: %s %s", name, issue.key, verdict.scores, verdict.reason)
                self.events.emit("judge_rejected", f"{name}: {verdict.reason} {verdict.scores}"[:300],
                                 issue, level="warn", source="llm")
                diagnosis.patch = None
                return
            self.events.emit("judge_passed", f"{name}: {verdict.scores}", issue, source="llm")
        reason = self._precheck(issue, plan, dep_obj)
        self._trace["precheck"] = reason or "ok"
        if reason:
            log.info("pre-check rejected %s for %s: %s", name, issue.key, reason)
            self.events.emit("action_rejected", f"{name}: {reason}", issue, level="warn", source="llm")
            return
        diagnosis.patch = plan.patch
        diagnosis.target_kind = "deployment"
        diagnosis.target_name = dep_name
        diagnosis.target_namespace = issue.namespace
        if _fix_key(diagnosis) in self._failed_fixes:
            self.events.emit("action_rejected", f"{name}: failed before, not retrying",
                             issue, level="warn", source="llm")
            diagnosis.patch = None
            return
        diagnosis.auto_fixable = True
        via = "web search + LLM" if used_web else "LLM"
        diagnosis.summary = f"{plan.summary} ({via}: {diagnosis.summary})"

    def _diagnosis_from_kb(self, issue: PodIssue, entry: KBEntry) -> Diagnosis:
        dep_name, _ = self.kube.get_owner_deployment(issue.namespace, issue.pod)
        # Workload-specific fixes (an image, an env value) must not leak onto
        # other Deployments: only auto-apply where the entry was verified.
        same_target = entry.target_name is None or entry.target_name == dep_name
        auto_fixable = bool(entry.patch and dep_name and same_target)
        patch = _refresh_restart_stamp(entry.patch)
        return Diagnosis(
            source=DiagnosisSource.KB,
            symptom=issue.symptom,
            root_cause=entry.root_cause,
            summary=entry.solution,
            actions=[entry.solution],
            confidence=entry.confidence,
            patch=patch,
            target_kind=entry.target_kind or "deployment",
            target_name=dep_name,
            target_namespace=issue.namespace,
            auto_fixable=auto_fixable,
        )

    def _precheck(self, issue: PodIssue, plan: "actions.ActionPlan", dep_obj) -> Optional[str]:
        """Verify an LLM guess against the live pod; returns a rejection reason or None."""
        if plan.action != "set_readiness_probe_path" or not self.config.action_precheck:
            return None
        pod_ip = (issue.raw or {}).get("status", {}).get("podIP")
        container = container_from_deployment(dep_obj, issue.container) or {}
        probe = (container.get("readinessProbe") or {}).get("httpGet") or {}
        port = probe.get("port")
        if isinstance(port, str):  # named port -> number
            port = next((p.get("containerPort") for p in container.get("ports", [])
                         if p.get("name") == port), None)
        path = plan.patch["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"]
        if not (pod_ip and port):
            return "cannot reach the pod to verify the new path"
        scheme = (probe.get("scheme") or "HTTP").lower()
        status = self.http_status(f"{scheme}://{pod_ip}:{port}{path}")
        if status is None or status >= 400:
            return f"new path {path} answered {status or 'nothing'} on the pod"
        return None

    @staticmethod
    def http_status(url: str) -> Optional[int]:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except OSError:
            return None

    def _report(self, issue: PodIssue, diagnosis: Diagnosis) -> None:
        log.info(
            "diagnosis[%s] %s | root_cause=%s | confidence=%.2f | actions=%s",
            diagnosis.source.value,
            issue.key,
            diagnosis.root_cause,
            diagnosis.confidence,
            ", ".join(diagnosis.actions),
        )


def _fix_key(diagnosis: Diagnosis) -> tuple[str, str, str]:
    """Identity of a fix attempt; catalog actions by name+params (restart patches carry a timestamp)."""
    target = f"{diagnosis.target_namespace}/{diagnosis.target_name}"
    if diagnosis.proposed_action:
        return (target, diagnosis.proposed_action, json.dumps(diagnosis.action_params, sort_keys=True))
    return (target, "patch", json.dumps(diagnosis.patch, sort_keys=True))


def _refresh_restart_stamp(patch):
    """A learned rollout_restart replays its old timestamp (a no-op); stamp it fresh."""
    annotations = (((patch or {}).get("spec") or {}).get("template") or {}).get("metadata", {}).get("annotations")
    if not annotations or "kubectl.kubernetes.io/restartedAt" not in annotations:
        return patch
    import copy
    from datetime import datetime, timezone

    fresh = copy.deepcopy(patch)
    fresh["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = (
        datetime.now(timezone.utc).isoformat())
    return fresh
