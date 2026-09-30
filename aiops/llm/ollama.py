"""Ollama-backed analyzer. Produces a root-cause + fix suggestion.

Note: LLM output is treated as a *recommendation only*. It is never applied
automatically because free-text fixes are unsafe. A human (or a later verified
KB entry) promotes it into a structured, auto-applicable patch.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

from ..config import Config
from ..models import Diagnosis, DiagnosisSource, PodIssue
from .base import LLMAnalyzer, LLMInput

log = logging.getLogger("aiops.llm.ollama")

_PROMPT = """You are a Kubernetes SRE. Diagnose the failing pod from the data
below. Respond with ONLY a JSON object of the form:
{{"root_cause": "...", "fix": "...", "confidence": 0.0}}
confidence is 0.0-1.0. Base the answer only on the data below; if the data
does not show the cause, say so and use a confidence below 0.5.

Symptom: {symptom}
Message: {message}

--- Pod Events ---
{events}

--- Container Logs (tail) ---
{logs}

--- Previous Logs ---
{previous}

--- kubectl describe pod ---
{describe}

--- Deployment YAML ---
{yaml}
"""


class OllamaAnalyzer(LLMAnalyzer):
    def __init__(self, config: Config) -> None:
        self.config = config

    def analyze(self, issue: PodIssue, data: LLMInput) -> Optional[Diagnosis]:
        prompt = _PROMPT.format(
            symptom=issue.symptom.value,
            message=issue.message,
            events=_truncate(data.events),
            logs=_truncate(data.logs),
            previous=_truncate(data.previous_logs),
            describe=_truncate(data.describe),
            yaml=_truncate(data.deployment_yaml),
        )
        payload = json.dumps(
            {
                "model": self.config.ollama_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.config.ollama_endpoint.rstrip("/") + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.config.llm_timeout_seconds
            ) as resp:
                raw = json.loads(resp.read().decode("utf-8")).get("response", "")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("Ollama call failed: %s", exc)
            return None

        parsed = _extract_json(raw)
        if not parsed:
            return None

        # Some models emit confidence as a percentage (90) rather than 0.90.
        raw_conf = float(parsed.get("confidence", 0.0) or 0.0)
        confidence = raw_conf / 100.0 if raw_conf > 1.0 else raw_conf

        fix = str(parsed.get("fix") or "").strip()
        if not fix or confidence < self.config.llm_min_confidence:
            log.info(
                "discarding LLM answer for %s (confidence=%.2f < %.2f or no fix)",
                issue.key, confidence, self.config.llm_min_confidence,
            )
            return None

        return Diagnosis(
            source=DiagnosisSource.LLM,
            symptom=issue.symptom,
            root_cause=parsed.get("root_cause", "unknown"),
            summary=fix,
            actions=[fix],
            confidence=confidence,
            auto_fixable=False,  # never auto-apply free-text LLM suggestions
        )


def _truncate(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _extract_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None
