"""Knowledge Base: structured, verified fix records (not Q&A)."""
from .base import KnowledgeBase
from .json_store import JsonlKnowledgeBase

__all__ = ["KnowledgeBase", "JsonlKnowledgeBase"]
