"""Learning mechanism: only verified + validated + stable fixes enter the KB."""
from __future__ import annotations

import logging

from .config import Config
from .knowledge_base import KnowledgeBase
from .models import (
    Diagnosis,
    KBEntry,
    PodIssue,
    RemediationResult,
    ValidationResult,
)

log = logging.getLogger("aiops.learning")


class LearningEngine:
    def __init__(self, kb: KnowledgeBase, config: Config) -> None:
        self.kb = kb
        self.config = config

    def record(
        self,
        issue: PodIssue,
        diagnosis: Diagnosis,
        remediation: RemediationResult,
        validation: ValidationResult,
    ) -> bool:
        # Guard: never store an unverified case (avoids the "explodes later" trap).
        if not (remediation.success and validation.success):
            return False

        entry = KBEntry(
            symptom=issue.symptom.value,
            problem=issue.message,
            root_cause=diagnosis.root_cause,
            solution=diagnosis.summary,
            verified=True,
            confidence=diagnosis.confidence,
            cluster=self.config.cluster_name,
            patch=diagnosis.patch,
            target_kind=diagnosis.target_kind,
        )
        self.kb.add(entry)
        log.info("learned verified fix for %s", issue.key)
        return True
