"""The watcher: scheduled checks, immediate re-checks, chat command handling."""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import airports
from .config import Config
from .metrics import METRICS
from .nlu import Command, parse
from .notify import Notifier
from .search import Offer, Searcher, google_flights_url
from .store import PricePoint, Store, Watch

log = logging.getLogger("flightwatch.service")

_WEEKDAYS = "一二三四五六日"

HELP_TEXT = (
    "直接打字告訴我就好，例如：\n"
    "・台北到東京（新增追蹤，會馬上查一次）\n"
    "・高雄飛大阪 12月 來回5天 5000以下\n"
    "・只追蹤台北到首爾（取代其他航線）\n"
    "・列表 ／ 立即查詢 ／ 刪除 #2 ／ #1 目標 4000\n"
    "之後每 {interval} 小時自動檢查，有新低價或達到目標價就通知你。"
)


class FlightWatcher:
    def __init__(self, config: Config, store: Store, searcher: Searcher, notifier: Notifier) -> None:
        self.config = config
        self.store = store
        self.searcher = searcher
        self.notifier = notifier
        self.tz = ZoneInfo(config.timezone)
        self._queue: "queue.Queue[tuple[int, str]]" = queue.Queue()
        self._stop = threading.Event()
        self.last_cycle_at: Optional[float] = None
        # Counters start at 0 so Prometheus' increase() sees the first event.
        for result in ("ok", "empty", "error"):
            METRICS.inc("flightwatch_checks_total", 0, result=result)
        for action in ("add", "replace", "remove", "list", "check", "set_target", "help", "unknown"):
            for parser in ("llm", "rules"):
                METRICS.inc("flightwatch_commands_total", 0, action=action, parser=parser)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # --- scheduling --------------------------------------------------------
    def request_check(self, watch_id: int, reason: str) -> None:
        self._queue.put((watch_id, reason))

    def due_watches(self, now: float) -> list[Watch]:
        interval = self.config.check_interval_minutes * 60
        return [
            w for w in self.store.list_watches()
            if w.last_checked_at is None or now - w.last_checked_at >= interval
        ]

    def run_forever(self) -> None:
        log.info("scheduler started: every %s min", self.config.check_interval_minutes)
        self.refresh_metrics()
        while not self._stop.is_set():
            try:
                watch_id, reason = self._queue.get(timeout=30)
                watch = self.store.get_watch(watch_id)
                if watch and watch.active:
                    self.check(watch, reason)
                continue
            except queue.Empty:
                pass
            self.last_cycle_at = time.time()
            for watch in self.due_watches(time.time()):
                if not self._queue.empty():
                    break  # a user request jumps the line
                self.check(watch, "scheduled")
                time.sleep(self.config.pause_between_watches_seconds)

    def stop(self) -> None:
        self._stop.set()

    # --- checking ----------------------------------------------------------
    def check(self, watch: Watch, reason: str) -> Optional[Offer]:
        start, end = watch.window(self.today(), self.config.window_start_days,
                                  self.config.window_end_days)
        log.info("checking #%s %s (%s) %s..%s [%s]", watch.id, watch.route,
                 watch.codes_label, start, end, reason)
        previous_low = self.store.lowest_price(watch.id)
        previous = self.store.latest_price(watch.id)
        try:
            offer = self.searcher.cheapest(watch, start, end)
        except Exception as exc:
            log.exception("check failed for #%s", watch.id)
            self.store.update_watch(watch.id, last_checked_at=time.time(), last_error=str(exc)[:300])
            METRICS.inc("flightwatch_checks_total", result="error")
            if reason != "scheduled":
                self._send(f"#{watch.id} {watch.route} 查詢失敗：{exc}", kind="error")
            return None

        self.store.update_watch(watch.id, last_checked_at=time.time(), last_error=None)
        if offer is None:
            METRICS.inc("flightwatch_checks_total", result="empty")
            log.info("no fares found for #%s", watch.id)
            if reason != "scheduled":
                self._send(f"#{watch.id} {watch.route}：這段日期查不到航班", kind="reply")
            self.refresh_metrics()
            return None

        METRICS.inc("flightwatch_checks_total", result="ok")
        self.store.add_price(PricePoint(
            watch_id=watch.id, checked_at=time.time(), price=offer.price,
            currency=offer.currency, travel_date=offer.travel_date,
            return_date=offer.return_date, airline=offer.airline, flight_no=offer.flight_no,
            depart_time=offer.depart_time, stops=offer.stops,
        ))
        log.info("#%s %s cheapest %s %.0f on %s (%s %s)", watch.id, watch.route,
                 offer.currency, offer.price, offer.travel_date, offer.airline, offer.flight_no)
        self.refresh_metrics()

        headline = self._alert_reason(watch, offer, previous_low, previous, reason)
        if headline:
            self._send(
                f"{headline}\n{self.format_offer(watch, offer, previous_low)}",
                title=f"✈️ {watch.route} NT${offer.price:,.0f}" if offer.currency == "TWD"
                else f"✈️ {watch.route} {offer.currency} {offer.price:,.0f}",
                kind="deal" if reason == "scheduled" else "reply",
                click=google_flights_url(watch, offer, self.config),
                priority=4 if reason == "scheduled" else 3,
            )
        return offer

    def _alert_reason(self, watch: Watch, offer: Offer, previous_low: Optional[float],
                      previous: Optional[PricePoint], reason: str) -> Optional[str]:
        if reason != "scheduled":
            return f"🔎 #{watch.id} 目前最低價"
        if previous_low is None:
            return f"🆕 #{watch.id} 開始追蹤"
        if offer.price <= previous_low * (1 - self.config.new_low_threshold):
            return f"📉 #{watch.id} 新低價！（之前最低 {self._money(previous_low, offer.currency)}）"
        if (watch.max_price and offer.price <= watch.max_price
                and (previous is None or previous.price > watch.max_price)):
            return f"🎯 #{watch.id} 達到目標價 {self._money(watch.max_price, offer.currency)}！"
        return None

    @staticmethod
    def _money(value: float, currency: str) -> str:
        return f"NT${value:,.0f}" if currency == "TWD" else f"{currency} {value:,.0f}"

    def format_offer(self, watch: Watch, offer: Offer, previous_low: Optional[float] = None) -> str:
        d = date.fromisoformat(offer.travel_date)
        when = f"{d.month}/{d.day}（{_WEEKDAYS[d.weekday()]}）"
        if offer.return_date:
            r = date.fromisoformat(offer.return_date)
            when += f" → {r.month}/{r.day}（{_WEEKDAYS[r.weekday()]}）來回"
        lines = [f"{watch.route}  {self._money(offer.price, offer.currency)}  {when}"]
        if offer.airline:
            stops = "直飛" if offer.stops == 0 else f"轉機 {offer.stops} 次"
            lines.append(f"{offer.airline} {offer.flight_no or ''} {stops} {offer.depart_time or ''}".strip())
        low = min(previous_low, offer.price) if previous_low is not None else offer.price
        lines.append(f"歷史最低 {self._money(low, offer.currency)}")
        if watch.max_price:
            lines.append(f"目標價 {self._money(watch.max_price, offer.currency)}")
        return "\n".join(lines)

    # --- chat --------------------------------------------------------------
    def handle_text(self, text: str, source: str) -> str:
        text = (text or "").strip()
        if not text:
            return "請輸入指令，輸入「說明」看範例。"
        self.store.log_message("in", source, text)
        cmd = parse(text, self.today(), self.config)
        METRICS.inc("flightwatch_commands_total", action=cmd.action, parser=cmd.parser)
        log.info("command from %s: %r -> %s", source, text, cmd)
        reply = self.execute(cmd)
        self._send(reply, kind="reply")
        return reply

    def execute(self, cmd: Command) -> str:
        if cmd.action == "help":
            return HELP_TEXT.format(interval=self.config.check_interval_minutes // 60 or 1)
        if cmd.action == "list":
            return self._list_text()
        if cmd.action in ("add", "replace"):
            return self._add(cmd)
        if cmd.action == "remove":
            return self._remove(cmd)
        if cmd.action == "check":
            targets = self._targets(cmd) or self.store.list_watches()
            for w in targets:
                self.request_check(w.id, "manual")
            if not targets:
                return "目前沒有追蹤任何航線。"
            return f"🔄 正在查詢 {len(targets)} 條航線，結果馬上通知你。"
        if cmd.action == "set_target":
            targets = self._targets(cmd)
            if not targets or cmd.max_price is None:
                return "要指定編號和價格，例如：#1 目標 4000"
            for w in targets:
                self.store.update_watch(w.id, max_price=cmd.max_price)
            self.refresh_metrics()
            return f"🎯 已把 {', '.join('#' + str(w.id) for w in targets)} 的目標價設為 NT${cmd.max_price:,.0f}"
        return "我看不太懂 🙏 " + HELP_TEXT.format(interval=self.config.check_interval_minutes // 60 or 1)

    def _targets(self, cmd: Command) -> list[Watch]:
        if cmd.watch_id is not None:
            w = self.store.get_watch(cmd.watch_id)
            return [w] if w and w.active else []
        if cmd.destination:
            origin = airports.resolve(cmd.origin or self.config.default_origin)
            dest = airports.resolve(cmd.destination)
            if origin and dest:
                return self.store.find_active(origin[1], dest[1])
        return []

    def _add(self, cmd: Command) -> str:
        origin = airports.resolve(cmd.origin or self.config.default_origin)
        dest = airports.resolve(cmd.destination or "")
        if not origin or not dest:
            return f"找不到機場：{cmd.origin or ''} → {cmd.destination or ''}。可以用中文城市名或 IATA 代碼（例如 NRT）。"
        o_codes, d_codes = airports.supported_codes(origin[1]), airports.supported_codes(dest[1])
        if not o_codes or not d_codes:
            return f"目前的航班資料來源不支援 {origin[0]} → {dest[0]} 的機場代碼。"

        replaced = []
        if cmd.action == "replace":
            for w in self.store.list_watches():
                self.store.update_watch(w.id, active=False)
                replaced.append(f"#{w.id}")

        for w in self.store.find_active(o_codes, d_codes):
            if (w.date_from, w.date_to, w.stay_days) == (cmd.date_from, cmd.date_to, cmd.stay_days):
                if cmd.max_price is not None:
                    self.store.update_watch(w.id, max_price=cmd.max_price)
                self.request_check(w.id, "manual")
                return f"👌 #{w.id} {w.route} 已經在追蹤了，馬上幫你重新查一次。"

        watch = self.store.add_watch(
            origin[0], o_codes, dest[0], d_codes,
            date_from=cmd.date_from, date_to=cmd.date_to, stay_days=cmd.stay_days,
            max_price=cmd.max_price, nonstop=cmd.nonstop,
        )
        self.request_check(watch.id, "added")
        self.refresh_metrics()
        parts = [f"✅ 已新增追蹤 #{watch.id} {watch.route}（{watch.codes_label}）",
                 f"日期：{watch.describe_window()}"]
        if watch.max_price:
            parts.append(f"目標價：NT${watch.max_price:,.0f}")
        if watch.nonstop:
            parts.append("只看直飛")
        if replaced:
            parts.append(f"已停止追蹤 {', '.join(replaced)}")
        parts.append(f"正在查詢目前最低價，之後每 {self.config.check_interval_minutes // 60 or 1} 小時自動檢查。")
        return "\n".join(parts)

    def _remove(self, cmd: Command) -> str:
        targets = self._targets(cmd)
        if not targets:
            return "找不到要刪除的航線，請用「列表」看編號，例如：刪除 #2"
        for w in targets:
            self.store.update_watch(w.id, active=False)
        self.refresh_metrics()
        return "🗑️ 已停止追蹤 " + "、".join(f"#{w.id} {w.route}" for w in targets)

    def _list_text(self) -> str:
        watches = self.store.list_watches()
        if not watches:
            return "目前沒有追蹤任何航線。打「台北到東京」試試看！"
        lines = ["📋 目前追蹤："]
        for w in watches:
            latest = self.store.latest_price(w.id)
            price = self._money(latest.price, latest.currency) if latest else "尚未查到"
            when = f" {latest.travel_date}" if latest else ""
            target = f" 目標 NT${w.max_price:,.0f}" if w.max_price else ""
            lines.append(f"#{w.id} {w.route}：{price}{when}（{w.describe_window()}）{target}")
        return "\n".join(lines)

    def _send(self, message: str, **kwargs) -> None:
        self.store.log_message("out", "bot", message)
        self.notifier.send(message, **kwargs)

    # --- metrics -----------------------------------------------------------
    def refresh_metrics(self) -> None:
        watches = self.store.list_watches()
        for name in ("flightwatch_price", "flightwatch_lowest_price",
                     "flightwatch_target_price", "flightwatch_last_check_timestamp_seconds"):
            METRICS.clear(name)
        METRICS.set("flightwatch_watches_active", len(watches))
        for w in watches:
            labels = {"watch_id": w.id, "route": w.route, "codes": w.codes_label}
            latest = self.store.latest_price(w.id)
            if latest:
                METRICS.set("flightwatch_price", latest.price, currency=latest.currency, **labels)
                METRICS.set("flightwatch_lowest_price", self.store.lowest_price(w.id) or latest.price,
                            currency=latest.currency, **labels)
            if w.max_price:
                METRICS.set("flightwatch_target_price", w.max_price, **labels)
            if w.last_checked_at:
                METRICS.set("flightwatch_last_check_timestamp_seconds", w.last_checked_at, **labels)
