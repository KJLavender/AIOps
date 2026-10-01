"""Natural-language chat command -> structured Command.

The LLM (Ollama) handles free phrasing; a deterministic rule parser covers the
common forms and takes over whenever the LLM is unreachable or its answer
doesn't resolve to real airports.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

from . import airports
from .config import Config

log = logging.getLogger("flightwatch.nlu")

ACTIONS = {"add", "replace", "remove", "list", "check", "set_target", "help", "unknown"}


@dataclass
class Command:
    action: str = "unknown"
    origin: Optional[str] = None
    destination: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    stay_days: Optional[int] = None
    max_price: Optional[float] = None
    watch_id: Optional[int] = None
    nonstop: bool = False
    parser: str = "rules"
    vague_year: bool = False  # "明年" without a month: ask which month


# --- rule parser ----------------------------------------------------------
_HELP_RE = re.compile(r"^\s*(help|說明|幫助|怎麼用|指令|用法|\?|？)\s*$", re.I)
_LIST_RE = re.compile(r"(列表|清單|list|目前(在)?追蹤|追蹤(了)?(哪些|什麼)|有哪些)", re.I)
_REMOVE_RE = re.compile(r"(刪除|取消|移除|停止追蹤|不要(再)?追蹤|不追了|remove|delete)", re.I)
_CHECK_RE = re.compile(r"(立即|馬上|現在|重新)(查詢|檢查|更新|查)|^\s*(查詢|檢查|更新|check)\s*", re.I)
_REPLACE_RE = re.compile(r"(只追蹤|只要|只看|改成只)")
_NONSTOP_RE = re.compile(r"(直飛|non-?stop)", re.I)
_ID_RE = re.compile(r"#\s*(\d+)")
_PRICE_RES = [
    re.compile(r"(?:低於|少於|便宜於|不超過|預算|目標價?|under|below|<)\s*(?:NT\$|\$|台幣)?\s*([\d,]{3,})", re.I),
    re.compile(r"([\d,]{3,})\s*(?:元|塊)?\s*(?:以下|以內)"),
]
_STAY_RES = [
    re.compile(r"(?:來回|玩|待|住)\s*(\d{1,2})\s*天"),
    re.compile(r"(\d{1,2})\s*天\s*(?:來回|行程)"),
]
_RANGE_MD_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s*[-~到至]\s*(\d{1,2})/(\d{1,2})")
_RANGE_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*[-~到至]\s*(\d{4}-\d{2}-\d{2})")
_MONTH_RE = re.compile(r"(?:(\d{4})\s*[/年.-]\s*|(明年)\s*)?(\d{1,2})\s*月")
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s*[/.-]\s*(\d{1,2})(?![\d/.-])")
_NEXT_YEAR_RE = re.compile(r"明年")
_IATA_RE = re.compile(r"\b[A-Z]{3}\b")


def _find_places(text: str) -> list[str]:
    """Known places in order of appearance (longest alias wins on overlap)."""
    lowered = text.lower()
    taken = [False] * len(text)
    hits: list[tuple[int, str]] = []
    for alias in airports.known_names():
        if alias.isascii():
            pattern = re.compile(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])")
        else:
            pattern = re.compile(re.escape(alias))
        for m in pattern.finditer(lowered):
            if any(taken[m.start():m.end()]):
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            hits.append((m.start(), alias))
    for m in _IATA_RE.finditer(text):
        if not any(taken[m.start():m.end()]) and airports.supported_codes([m.group(0)]):
            hits.append((m.start(), m.group(0)))
    return [alias for _, alias in sorted(hits)]


def _next_date(today: date, month: int, day: int) -> date:
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


def _parse_dates(text: str, today: date) -> tuple[Optional[str], Optional[str]]:
    m = _RANGE_ISO_RE.search(text)
    if m:
        return m.group(1), m.group(2)
    m = _RANGE_MD_RE.search(text)
    if m:
        start = _next_date(today, int(m.group(1)), int(m.group(2)))
        end = date(start.year, int(m.group(3)), int(m.group(4)))
        if end < start:
            end = date(start.year + 1, end.month, end.day)
        return start.isoformat(), end.isoformat()
    m = _MONTH_RE.search(text)
    if m and 1 <= int(m.group(3)) <= 12:
        month = int(m.group(3))
        if m.group(1):
            year = int(m.group(1))
        elif m.group(2):
            year = today.year + 1
        else:
            year = today.year if month >= today.month else today.year + 1
        return _month_window(year, month)
    m = _YEAR_MONTH_RE.search(text)
    if m and 1 <= int(m.group(2)) <= 12:
        return _month_window(int(m.group(1)), int(m.group(2)))
    return None, None


def _month_window(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last).isoformat()


def rule_parse(text: str, today: date, default_origin: str) -> Command:
    cmd = Command(parser="rules")
    if _HELP_RE.match(text):
        cmd.action = "help"
        return cmd
    id_match = _ID_RE.search(text)
    cmd.watch_id = int(id_match.group(1)) if id_match else None
    for pattern in _PRICE_RES:
        m = pattern.search(text)
        if m:
            cmd.max_price = float(m.group(1).replace(",", ""))
            break
    for pattern in _STAY_RES:
        m = pattern.search(text)
        if m:
            cmd.stay_days = int(m.group(1))
            break
    cmd.date_from, cmd.date_to = _parse_dates(text, today)
    cmd.vague_year = bool(_NEXT_YEAR_RE.search(text)) and cmd.date_from is None
    cmd.nonstop = bool(_NONSTOP_RE.search(text))
    places = _find_places(text)
    if len(places) >= 2:
        cmd.origin, cmd.destination = places[0], places[1]
    elif len(places) == 1:
        cmd.origin, cmd.destination = default_origin, places[0]

    if _REMOVE_RE.search(text):
        cmd.action = "remove"
    elif _LIST_RE.search(text) and not places:
        cmd.action = "list"
    elif places:
        cmd.action = "replace" if _REPLACE_RE.search(text) else "add"
    elif cmd.watch_id is not None and cmd.max_price is not None:
        cmd.action = "set_target"
    elif _CHECK_RE.search(text):
        cmd.action = "check"
    return cmd


# --- LLM parser -----------------------------------------------------------
_PROMPT = """你是機票追蹤機器人的指令解析器。把使用者的訊息轉成 JSON，只輸出 JSON：
{{"action": "add|replace|remove|list|check|set_target|help|unknown",
  "origin": "出發城市或機場代碼，沒提到就用 null",
  "destination": "目的地城市或機場代碼，沒提到就用 null",
  "date_from": "YYYY-MM-DD 或 null", "date_to": "YYYY-MM-DD 或 null",
  "stay_days": 來回天數整數或 null, "max_price": 目標價格數字或 null,
  "watch_id": 追蹤編號整數或 null, "nonstop": true/false}}

規則：
- 想追蹤、查、看某條航線的優惠（包含「改成」「換成」某目的地）→ add（額外新增，不刪舊的）
- 明確說「只追蹤/只要」某航線 → replace
- 刪除/取消/不要追蹤 → remove；列出目前追蹤 → list；立即查詢 → check
- 只改某個編號的目標價 → set_target
- 只提目的地時 origin 用 null。城市名保持使用者寫的中文。
- 今天是 {today}。「12月」代表今天之後最近的 12 月整個月。

使用者訊息：{text}
"""


def llm_parse(text: str, today: date, config: Config) -> Optional[Command]:
    if not config.ollama_endpoint:
        return None
    payload = json.dumps({
        "model": config.ollama_model,
        "prompt": _PROMPT.format(today=today.isoformat(), text=text),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(
        config.ollama_endpoint.rstrip("/") + "/api/generate",
        data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=config.llm_timeout_seconds) as resp:
            raw = json.loads(resp.read().decode("utf-8")).get("response", "")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        log.warning("LLM parse failed, using rules: %s", exc)
        return None
    action = str(data.get("action") or "unknown")
    if action not in ACTIONS:
        return None

    def _num(key, cast):
        try:
            return cast(data[key]) if data.get(key) not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    def _date(key):
        val = data.get(key)
        try:
            return date.fromisoformat(val).isoformat() if val else None
        except (TypeError, ValueError):
            return None

    return Command(
        action=action,
        origin=data.get("origin") or None,
        destination=data.get("destination") or None,
        date_from=_date("date_from"),
        date_to=_date("date_to"),
        stay_days=_num("stay_days", int),
        max_price=_num("max_price", float),
        watch_id=_num("watch_id", int),
        nonstop=bool(data.get("nonstop")),
        parser="llm",
    )


def parse(text: str, today: date, config: Config) -> Command:
    rules = rule_parse(text, today, config.default_origin)
    llm = llm_parse(text, today, config)
    if llm is None or llm.action == "unknown":
        return rules
    if llm.action in ("add", "replace"):
        llm.origin = llm.origin or config.default_origin
        if not (airports.resolve(llm.origin) and llm.destination and airports.resolve(llm.destination)):
            log.info("LLM places unresolvable (%s -> %s); using rules", llm.origin, llm.destination)
            return rules
    # Regex-extracted details are exact (the LLM once read "12月" as ending
    # 2027-01-01), so they win; the LLM keeps what the rules couldn't see.
    for key in ("date_from", "date_to", "stay_days", "max_price", "nonstop"):
        if getattr(rules, key) not in (None, False):
            setattr(llm, key, getattr(rules, key))
    if rules.action == llm.action:
        for key, value in asdict(rules).items():
            if key != "parser" and getattr(llm, key) in (None, False) and value not in (None, False):
                setattr(llm, key, value)
    return llm
