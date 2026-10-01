"""Tiny Prometheus text-format registry (stdlib only)."""
from __future__ import annotations

import threading

_HELP = {
    "flightwatch_checks_total": ("counter", "Price checks by result"),
    "flightwatch_commands_total": ("counter", "Chat commands by action and parser"),
    "flightwatch_notifications_total": ("counter", "Outgoing notifications by kind and result"),
    "flightwatch_price": ("gauge", "Latest cheapest fare found for a watched route"),
    "flightwatch_lowest_price": ("gauge", "Lowest fare ever recorded for a watched route"),
    "flightwatch_target_price": ("gauge", "User's target fare for a watched route"),
    "flightwatch_last_check_timestamp_seconds": ("gauge", "Unix time of the last check per route"),
    "flightwatch_watches_active": ("gauge", "Number of active watched routes"),
}


def _number(value: float) -> str:
    # Full precision: "%g" would turn a Unix timestamp into 1.79079e+09 (off by hours).
    return str(int(value)) if value.is_integer() else repr(value)


def _key(labels: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, dict[tuple, float]] = {}

    def inc(self, name: str, amount: float = 1.0, **labels) -> None:
        with self._lock:
            series = self._values.setdefault(name, {})
            series[_key(labels)] = series.get(_key(labels), 0.0) + amount

    def set(self, name: str, value: float, **labels) -> None:
        with self._lock:
            self._values.setdefault(name, {})[_key(labels)] = float(value)

    def clear(self, name: str) -> None:
        with self._lock:
            self._values.pop(name, None)

    def get(self, name: str, **labels) -> float:
        with self._lock:
            return self._values.get(name, {}).get(_key(labels), 0.0)

    def render(self) -> str:
        lines = []
        with self._lock:
            for name, series in sorted(self._values.items()):
                kind, text = _HELP.get(name, ("untyped", name))
                lines.append(f"# HELP {name} {text}")
                lines.append(f"# TYPE {name} {kind}")
                for labels, value in sorted(series.items()):
                    label_text = ",".join(
                        f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                        for k, v in labels
                    )
                    text = _number(value)
                    lines.append(f"{name}{{{label_text}}} {text}" if label_text else f"{name} {text}")
        return "\n".join(lines) + "\n"


METRICS = Registry()
