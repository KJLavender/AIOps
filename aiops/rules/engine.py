"""The Rule Engine: first matching rule wins."""
from __future__ import annotations

import logging
from typing import Optional

from ..models import Diagnosis, PodIssue
from .base import Rule, RuleContext
from .builtin import CrashLoopBackOffRule, ImagePullRule, OOMKilledRule

log = logging.getLogger("aiops.rules.engine")


def default_rules() -> list[Rule]:
    return [OOMKilledRule(), ImagePullRule(), CrashLoopBackOffRule()]


class RuleEngine:
    def __init__(self, rules: Optional[list[Rule]] = None) -> None:
        self.rules = rules if rules is not None else default_rules()

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        for rule in self.rules:
            if rule.matches(issue):
                log.info("rule '%s' matched %s", rule.name, issue.key)
                return rule.diagnose(issue, ctx)
        return None
