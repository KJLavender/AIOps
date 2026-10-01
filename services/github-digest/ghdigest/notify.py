"""ntfy publish + subscribe (chat commands), and a tiny Prometheus registry."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("ghdigest.notify")

BOT_TAG = "robot"  # marks our own messages so the listener skips them


def publish(base_url: str, topic: str, title: str, message: str,
            click: Optional[str] = None, tags: Optional[list[str]] = None) -> bool:
    if not base_url:
        return False
    body = {"topic": topic, "title": title, "message": message, "tags": [BOT_TAG] + (tags or [])}
    if click:
        body["click"] = click
    req = urllib.request.Request(
        base_url.rstrip("/") + "/", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except OSError as exc:
        log.warning("ntfy publish failed: %s", exc)
        return False


class Listener:
    """Streams `<server>/<topic>/json`; every non-bot message goes to `handler`."""

    def __init__(self, base_url: str, topic: str, handler: Callable[[str, str], str]) -> None:
        self.url = f"{base_url.rstrip('/')}/{topic}/json"
        self.handler = handler
        self.since = "none"  # ntfy rejects since=now

    def run_forever(self) -> None:
        backoff = 2
        while True:
            try:
                with urllib.request.urlopen(f"{self.url}?since={self.since}", timeout=120) as resp:
                    backoff = 2
                    for raw in resp:
                        self.on_line(raw)
            except OSError as exc:
                log.warning("ntfy stream dropped (%s); retry in %ss", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def on_line(self, raw: bytes) -> None:
        try:
            event = json.loads(raw.decode("utf-8"))
        except ValueError:
            return
        if event.get("event") != "message":
            return
        self.since = event.get("id", self.since)
        if BOT_TAG in (event.get("tags") or []):
            return
        text = (event.get("message") or "").strip()
        if text:
            try:
                self.handler(text, "ntfy")
            except Exception:
                log.exception("command failed: %r", text)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, tuple], float] = {}
        self._kinds: dict[str, str] = {}

    def inc(self, name: str, amount: float = 1, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._kinds[name] = "counter"
            self._values[key] = self._values.get(key, 0) + amount

    def set(self, name: str, value: float, **labels) -> None:
        with self._lock:
            self._kinds[name] = "gauge"
            self._values[(name, tuple(sorted(labels.items())))] = float(value)

    def render(self) -> str:
        lines, done = [], set()
        with self._lock:
            for (name, labels), value in sorted(self._values.items()):
                if name not in done:
                    lines.append(f"# TYPE {name} {self._kinds[name]}")
                    done.add(name)
                text = str(int(value)) if float(value).is_integer() else repr(value)
                label_text = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{label_text}}} {text}" if label_text else f"{name} {text}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()
