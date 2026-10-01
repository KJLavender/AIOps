"""Daily scheduler, manual runs, and chat commands."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import jobs
from .config import Config
from .github import GitHubClient, Repo
from .notify import METRICS, publish
from .store import Store

log = logging.getLogger("ghdigest.service")

_ADD_RE = re.compile(r"^(?:新增主題|加入主題|新增|加入|add)\s*[:：]?\s*(.+)$", re.IGNORECASE)
_REMOVE_RE = re.compile(r"(?:刪除|移除|remove|delete)\D*(\d+)", re.IGNORECASE)
_LIST_RE = re.compile(r"^(?:主題|列表|清單|topics?|list)$", re.IGNORECASE)
_RUN_RE = re.compile(r"^(?:立即|馬上|現在)?(?:推薦|整理|執行|更新)$|^run$", re.IGNORECASE)
_HELP_RE = re.compile(r"^(?:說明|幫助|help|\?|？)$", re.IGNORECASE)


class DigestService:
    def __init__(self, config: Config, store: Store, client: GitHubClient) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.tz = ZoneInfo(config.timezone)
        self._lock = threading.Lock()  # one run at a time
        self.last_error: Optional[str] = None
        if config.mode == "research-picks" and not store.topics():
            for topic in config.default_topics:
                store.add_topic(topic)
        for result in ("ok", "error"):
            METRICS.inc("ghdigest_runs_total", 0, mode=config.mode, result=result)
        self._refresh_gauges()

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().date().isoformat()

    # --- scheduling --------------------------------------------------------
    def next_run(self, now: datetime) -> datetime:
        hour, minute = (int(x) for x in self.config.run_at.split(":"))
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)

    def due_on_start(self, now: datetime) -> bool:
        """Catch up after downtime: today's digest is missing and its time has passed."""
        hour, minute = (int(x) for x in self.config.run_at.split(":"))
        passed = (now.hour, now.minute) >= (hour, minute)
        return passed and self.store.digest(now.date().isoformat()) is None

    def run_forever(self) -> None:
        if self.due_on_start(self.now()):
            self.run("catch-up")
        while True:
            target = self.next_run(self.now())
            log.info("next %s digest at %s", self.config.mode, target.isoformat())
            while self.now() < target:
                time.sleep(min(60.0, max(1.0, (target - self.now()).total_seconds())))
            self.run("scheduled")

    # --- running -----------------------------------------------------------
    def run(self, reason: str) -> list[Repo]:
        with self._lock:
            day = self.today()
            log.info("running %s digest for %s [%s]", self.config.mode, day, reason)
            try:
                if self.config.mode == "daily-top":
                    repos = jobs.daily_top(self.client, self.config, self.now())
                else:
                    repos = jobs.research_picks(self.client, self.config, self.store, self.now())
            except Exception as exc:
                log.exception("digest failed")
                self.last_error = str(exc)[:300]
                METRICS.inc("ghdigest_runs_total", mode=self.config.mode, result="error")
                return []
            self.last_error = None
            self.store.save_digest(day, repos)
            METRICS.inc("ghdigest_runs_total", mode=self.config.mode, result="ok")
            METRICS.set("ghdigest_last_run_timestamp_seconds", time.time(), mode=self.config.mode)
            self._refresh_gauges()
            title, message = jobs.format_digest(self.config.mode, repos, day)
            publish(self.config.ntfy_url, self.config.ntfy_topic, title, message,
                    click=self.config.public_url or None, tags=["star"])
            log.info("%s digest: %d repos", self.config.mode, len(repos))
            return repos

    def run_async(self, reason: str) -> None:
        threading.Thread(target=self.run, args=(reason,), daemon=True).start()

    def _refresh_gauges(self) -> None:
        latest = self.store.recent_digests(1)
        METRICS.set("ghdigest_items", len(latest[0]["items"]) if latest else 0, mode=self.config.mode)
        if self.config.mode == "research-picks":
            METRICS.set("ghdigest_topics", len(self.store.topics()), mode=self.config.mode)

    # --- chat --------------------------------------------------------------
    def handle_text(self, text: str, source: str) -> str:
        text = (text or "").strip()
        reply = self._command(text)
        log.info("command from %s: %r", source, text)
        publish(self.config.ntfy_url, self.config.ntfy_topic, "🤖 GitHub 摘要", reply)
        return reply

    def _command(self, text: str) -> str:
        picks = self.config.mode == "research-picks"
        if _HELP_RE.match(text):
            if picks:
                return ("指令：「新增主題 eBPF」「刪除主題 #2」「主題」「立即推薦」。"
                        f"每天 {self.config.run_at} 依主題推薦 {self.config.picks_n} 個沒推薦過的 repo。")
            return f"每天 {self.config.run_at} 整理過去 24 小時新建立、星數前 {self.config.top_n} 的 repo。指令：「立即整理」。"
        if _RUN_RE.match(text):
            self.run_async("manual")
            return "🔄 開始整理，完成後會通知你（研究推薦約需 1–2 分鐘）。"
        if picks and _LIST_RE.match(text):
            topics = self.store.topics()
            return "📋 研究主題：\n" + "\n".join(f"#{i} {q}" for i, q in topics) if topics else "目前沒有主題。"
        if picks:
            m = _REMOVE_RE.search(text)
            if m:
                removed = self.store.remove_topic(int(m.group(1)))
                self._refresh_gauges()
                return f"🗑️ 已刪除主題「{removed}」" if removed else "找不到這個編號，輸入「主題」看列表。"
            m = _ADD_RE.match(text)
            if m:
                query = m.group(1).strip()
                added = self.store.add_topic(query)
                self._refresh_gauges()
                return f"✅ 已新增主題「{query}」，明天起會一起推薦（或輸入「立即推薦」）。" if added \
                    else f"「{query}」已經在主題裡了。"
        return "看不懂這個指令，輸入「說明」看範例。"
