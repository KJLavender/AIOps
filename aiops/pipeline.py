"""The MVP pipeline:

  Pod fails -> Rule Engine -> collect Logs -> KB Search -> LLM -> fix
            -> Auto Remediation -> Validation -> Learn
"""
from __future__ import annotations

import logging
from typing import Optional

from .collector import Collector
from .config import Config
from .events import EventLog
from .kube import KubeClient
from .knowledge_base import KnowledgeBase
from .learning import LearningEngine
from .llm import LLMAnalyzer, LLMInput
from .models import Diagnosis, DiagnosisSource, KBEntry, PodIssue
from .remediation import Remediator
from .rules import RuleEngine
from .rules.base import RuleContext
from .validation import Validator

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

    def handle(self, issue: PodIssue) -> None:
        log.info("handling issue %s: %s", issue.key, issue.message.strip()[:120])
        self.events.emit("detected", issue.message.strip()[:200], issue, level="warn")
        ctx = self._build_context(issue)

        diagnosis = self.rule_engine.diagnose(issue, ctx)

        # KB / LLM fallback when there's no rule, or the rule defers to them.
        if diagnosis is None or (
            not diagnosis.auto_fixable and diagnosis.forward_to_kb
        ):
            fallback = self._search_kb_or_llm(issue, ctx)
            if fallback is not None:
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
            log.info("[recommendation only] %s -> %s", issue.key, diagnosis.summary)
            self.events.emit("recommended", diagnosis.summary, issue, level="warn", source=source)
            return

        if not self.config.auto_fix:
            log.info("auto_fix disabled; skipping remediation for %s", issue.key)
            self.events.emit(
                "recommended", f"auto-fix disabled: {diagnosis.summary}", issue,
                level="warn", source=source,
            )
            return

        remediation = self.remediator.remediate(diagnosis)
        if not remediation.success:
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
            self.learning.record(issue, diagnosis, remediation, validation)
            log.info("RESOLVED %s (%s)", issue.key, validation.detail)
            self.events.emit("validated", validation.detail, issue, level="ok", source=source)
            self.events.emit("learned", "verified fix stored in KB", issue, level="ok", source=source)
        else:
            log.error("validation failed for %s: %s", issue.key, validation.detail)
            self.events.emit(
                "validation_failed", validation.detail, issue, level="error", source=source
            )

    # --- helpers -----------------------------------------------------------
    def _build_context(self, issue: PodIssue) -> RuleContext:
        ctx = RuleContext(kube=self.kube, config=self.config)
        # Only pull logs/events for symptoms that need them (avoids noise/cost).
        needs_logs = issue.symptom.value in {"CrashLoopBackOff", "Failed"}
        if needs_logs:
            ctx.logs = self.kube.pod_logs(issue.namespace, issue.pod, issue.container)
            ctx.previous_logs = self.kube.pod_logs(
                issue.namespace, issue.pod, issue.container, previous=True
            )
            ctx.events = self.kube.pod_events(issue.namespace, issue.pod)
            ctx.describe = self.kube.describe_pod(issue.namespace, issue.pod)
        return ctx

    def _search_kb_or_llm(
        self, issue: PodIssue, ctx: RuleContext
    ) -> Optional[Diagnosis]:
        query_text = f"{ctx.logs}\n{ctx.events}"
        hit = self.kb.search(issue, query_text)
        if hit is not None:
            return self._diagnosis_from_kb(issue, hit)

        if not self.config.llm_enabled:
            return None

        _, dep_obj = ctx.owner_deployment(issue)
        data = LLMInput(
            logs=ctx.logs,
            previous_logs=ctx.previous_logs,
            events=ctx.events,
            describe=ctx.describe,
            deployment_yaml=str(dep_obj) if dep_obj else "",
        )
        return self.llm.analyze(issue, data)

    def _diagnosis_from_kb(self, issue: PodIssue, entry: KBEntry) -> Diagnosis:
        dep_name, _ = self.kube.get_owner_deployment(issue.namespace, issue.pod)
        auto_fixable = bool(entry.patch and dep_name)
        return Diagnosis(
            source=DiagnosisSource.KB,
            symptom=issue.symptom,
            root_cause=entry.root_cause,
            summary=entry.solution,
            actions=[entry.solution],
            confidence=entry.confidence,
            patch=entry.patch,
            target_kind=entry.target_kind or "deployment",
            target_name=dep_name,
            target_namespace=issue.namespace,
            auto_fixable=auto_fixable,
        )

    def _report(self, issue: PodIssue, diagnosis: Diagnosis) -> None:
        log.info(
            "diagnosis[%s] %s | root_cause=%s | confidence=%.2f | actions=%s",
            diagnosis.source.value,
            issue.key,
            diagnosis.root_cause,
            diagnosis.confidence,
            ", ".join(diagnosis.actions),
        )
