"""Runtime configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _list(name: str) -> list[str]:
    return [v.strip() for v in (os.getenv(name) or "").split(",") if v.strip()]


@dataclass
class Config:
    db_path: str = "data/flightwatch.db"
    port: int = 8080
    timezone: str = "Asia/Taipei"

    # --- Schedule ---
    check_interval_minutes: int = 180  # every 3h: fresh enough, gentle on Google
    pause_between_watches_seconds: float = 3.0

    # --- Search defaults ---
    currency: str = "TWD"
    language: str = "zh-TW"
    country: str = "TW"
    default_origin: str = "台北"
    window_start_days: int = 7  # rolling window when the user gives no dates
    window_end_days: int = 90

    # --- Alerts ---
    new_low_threshold: float = 0.03  # notify when >=3% below the previous low

    # --- Notifications / chat (ntfy) ---
    ntfy_url: str = ""  # e.g. http://ntfy.travel.svc.cluster.local
    ntfy_topic: str = "flights"
    extra_notify_urls: list[str] = field(default_factory=list)  # full topic URLs

    # --- Natural-language commands (Ollama) ---
    ollama_endpoint: str = ""
    ollama_model: str = "qwen3.5:4b"
    llm_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=os.getenv("DB_PATH", "data/flightwatch.db"),
            port=_int("PORT", 8080),
            timezone=os.getenv("TZ", "Asia/Taipei"),
            check_interval_minutes=_int("CHECK_INTERVAL_MINUTES", 180),
            pause_between_watches_seconds=_float("PAUSE_BETWEEN_WATCHES", 3.0),
            currency=os.getenv("CURRENCY", "TWD"),
            language=os.getenv("LANGUAGE", "zh-TW"),
            country=os.getenv("COUNTRY", "TW"),
            default_origin=os.getenv("DEFAULT_ORIGIN", "台北"),
            window_start_days=_int("WINDOW_START_DAYS", 7),
            window_end_days=_int("WINDOW_END_DAYS", 90),
            new_low_threshold=_float("NEW_LOW_THRESHOLD", 0.03),
            ntfy_url=os.getenv("NTFY_URL", ""),
            ntfy_topic=os.getenv("NTFY_TOPIC", "flights"),
            extra_notify_urls=_list("EXTRA_NOTIFY_URLS"),
            ollama_endpoint=os.getenv("OLLAMA_ENDPOINT", ""),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:4b"),
            llm_timeout_seconds=_int("LLM_TIMEOUT", 60),
        )
