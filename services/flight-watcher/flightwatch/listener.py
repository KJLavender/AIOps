"""Chat input over ntfy: subscribe to the topic and treat user messages as commands."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

from .notify import BOT_TAG

log = logging.getLogger("flightwatch.listener")


class NtfyListener:
    """Streams `<server>/<topic>/json`; every non-bot message goes to `handler`."""

    def __init__(self, base_url: str, topic: str, handler) -> None:
        self.base_url = base_url.rstrip("/")
        self.topic = topic
        self.handler = handler
        # "none" = no backlog on first connect; afterwards resume from the last
        # seen message id so nothing sent during a reconnect is lost.
        self.since = "none"
        self._stop = threading.Event()

    def run_forever(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            url = f"{self.base_url}/{self.topic}/json?since={self.since}"
            try:
                # ntfy sends a keepalive every ~45s, so a read timeout means a dead link.
                with urllib.request.urlopen(url, timeout=120) as resp:
                    log.info("listening for commands on %s/%s", self.base_url, self.topic)
                    backoff = 2
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        self._on_line(raw)
            except OSError as exc:
                log.warning("ntfy stream dropped (%s); reconnecting in %ss", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _on_line(self, raw: bytes) -> None:
        try:
            event = json.loads(raw.decode("utf-8"))
        except ValueError:
            return
        if event.get("event") != "message":
            return
        self.since = event.get("id", self.since)  # resume here after a reconnect
        if BOT_TAG in (event.get("tags") or []):
            return  # our own reply / alert
        text = (event.get("message") or "").strip()
        if text:
            try:
                self.handler(text, "ntfy")
            except Exception:
                log.exception("command handling failed for %r", text)

    def stop(self) -> None:
        self._stop.set()
