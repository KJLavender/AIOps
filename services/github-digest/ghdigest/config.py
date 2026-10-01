"""Runtime configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_TOPICS = [
    # Seeded from this repo's own research: self-healing Kubernetes with LLMs.
    "aiops",
    "kubernetes self-healing",
    "llm sre agent",
    "kubernetes llm",
    "observability opentelemetry",
]


@dataclass
class Config:
    mode: str = "daily-top"  # daily-top | research-picks
    db_path: str = "data/ghdigest.db"
    port: int = 8080
    timezone: str = "Asia/Taipei"
    run_at: str = "09:00"  # local time, once a day
    github_token: str = ""

    top_n: int = 10  # daily-top: how many new repos
    picks_n: int = 5  # research-picks: recommendations per day
    default_topics: list[str] = field(default_factory=lambda: list(DEFAULT_TOPICS))

    ntfy_url: str = ""
    ntfy_topic: str = "github"
    public_url: str = ""  # link in notifications (this service's web UI)

    ollama_endpoint: str = ""
    ollama_model: str = "qwen2.5:7b"
    llm_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        topics = [t.strip() for t in (os.getenv("DEFAULT_TOPICS") or "").split(",") if t.strip()]
        return cls(
            mode=os.getenv("MODE", "daily-top"),
            db_path=os.getenv("DB_PATH", "data/ghdigest.db"),
            port=int(os.getenv("PORT", "8080")),
            timezone=os.getenv("TZ", "Asia/Taipei"),
            run_at=os.getenv("RUN_AT", "09:00"),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            top_n=int(os.getenv("TOP_N", "10")),
            picks_n=int(os.getenv("PICKS_N", "5")),
            default_topics=topics or list(DEFAULT_TOPICS),
            ntfy_url=os.getenv("NTFY_URL", ""),
            ntfy_topic=os.getenv("NTFY_TOPIC", "github"),
            public_url=os.getenv("PUBLIC_URL", ""),
            ollama_endpoint=os.getenv("OLLAMA_ENDPOINT", ""),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT", "60")),
        )
