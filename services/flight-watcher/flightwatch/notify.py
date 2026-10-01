"""Outgoing messages via ntfy (self-hosted in-cluster, plus optional extra topics)."""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

from .config import Config
from .metrics import METRICS

log = logging.getLogger("flightwatch.notify")

# Every bot message carries this tag so the command listener can skip its own
# replies on the shared chat topic.
BOT_TAG = "robot"


class Notifier:
    def __init__(self, config: Config) -> None:
        self.config = config

    def targets(self) -> list[tuple[str, str]]:
        """(server base URL, topic) pairs."""
        out = []
        if self.config.ntfy_url:
            out.append((self.config.ntfy_url.rstrip("/"), self.config.ntfy_topic))
        for url in self.config.extra_notify_urls:
            base, _, topic = url.rstrip("/").rpartition("/")
            if base and topic:
                out.append((base, topic))
        return out

    def send(
        self,
        message: str,
        title: str = "✈️ 機票追蹤",
        kind: str = "reply",
        click: Optional[str] = None,
        priority: int = 3,
    ) -> int:
        """Publish to every target; returns how many succeeded."""
        sent = 0
        for base, topic in self.targets():
            body = {
                "topic": topic,
                "title": title,
                "message": message,
                "tags": [BOT_TAG] + (["airplane"] if kind == "deal" else []),
                "priority": priority,
            }
            if click:
                body["click"] = click
            # JSON publishing to the server root keeps non-ASCII titles intact
            # (HTTP headers would need RFC 2047 encoding).
            req = urllib.request.Request(
                base + "/",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                sent += 1
            except OSError as exc:
                log.warning("ntfy publish to %s/%s failed: %s", base, topic, exc)
        METRICS.inc("flightwatch_notifications_total", kind=kind, result="ok" if sent else "failed")
        return sent
