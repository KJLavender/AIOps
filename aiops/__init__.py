"""AI Ops Agent for Kubernetes.

A self-healing pipeline that detects abnormal Pods, diagnoses them with a
Rule Engine (Phase 1), falls back to a Knowledge Base and optionally an LLM,
auto-remediates, validates, and learns verified fixes.
"""

__version__ = "0.1.0"
