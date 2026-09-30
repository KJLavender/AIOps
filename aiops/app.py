"""Wires the components together and runs the watch loop."""
from __future__ import annotations

import logging
import time

from .collector import Collector
from .config import Config
from .knowledge_base import JsonlKnowledgeBase
from .kube import KubeClient
from .learning import LearningEngine
from .llm import NullAnalyzer, OllamaAnalyzer
from .pipeline import Pipeline
from .remediation import Remediator
from .rules import RuleEngine
from .validation import Validator

log = logging.getLogger("aiops.app")


def build_pipeline(config: Config, kube=None) -> tuple[Collector, Pipeline]:
    kube = kube if kube is not None else KubeClient(config)
    collector = Collector(kube, config)
    kb = JsonlKnowledgeBase(config.kb_path, config.kb_score_threshold)
    llm = OllamaAnalyzer(config) if config.llm_enabled else NullAnalyzer()
    pipeline = Pipeline(
        kube=kube,
        config=config,
        rule_engine=RuleEngine(),
        kb=kb,
        llm=llm,
        remediator=Remediator(kube, config),
        validator=Validator(kube, config),
        learning=LearningEngine(kb, config),
    )
    return collector, pipeline


class Agent:
    def __init__(self, config: Config, kube=None) -> None:
        self.config = config
        self.collector, self.pipeline = build_pipeline(config, kube=kube)
        self._last_handled: dict[str, float] = {}

    def _on_cooldown(self, key: str) -> bool:
        last = self._last_handled.get(key)
        if last is None:
            return False
        return (time.time() - last) < self.config.handle_cooldown_seconds

    def tick(self) -> int:
        issues = self.collector.scan()
        handled = 0
        for issue in issues:
            if self._on_cooldown(issue.key):
                log.debug("cooldown active for %s; skipping", issue.key)
                continue
            self._last_handled[issue.key] = time.time()
            try:
                self.pipeline.handle(issue)
                handled += 1
            except Exception:  # keep the loop alive on unexpected errors
                log.exception("error handling %s", issue.key)
        return handled

    def run_forever(self) -> None:
        log.info(
            "AI Ops Agent started (auto_fix=%s, dry_run=%s, llm=%s)",
            self.config.auto_fix,
            self.config.dry_run,
            self.config.llm_enabled,
        )
        while True:
            try:
                found = self.tick()
                if not found:
                    log.debug("no active issues")
            except Exception:
                log.exception("scan cycle failed")
            time.sleep(self.config.poll_interval_seconds)
