import time
from datetime import date

from flightwatch.config import Config
from flightwatch.metrics import METRICS
from flightwatch.search import Offer
from flightwatch.service import FlightWatcher
from flightwatch.store import Store


class _Searcher:
    def __init__(self, prices):
        self.prices = list(prices)
        self.calls = []

    def cheapest(self, watch, start, end):
        self.calls.append((watch.id, start, end))
        price = self.prices.pop(0)
        if isinstance(price, Exception):
            raise price
        if price is None:
            return None
        return Offer(price=price, currency="TWD", travel_date="2026-10-31",
                     airline="Peach Aviation", flight_no="MM628", depart_time="17:15", stops=0)


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, message, **kwargs):
        self.sent.append((message, kwargs))
        return 1


def _watcher(prices=(), **config):
    cfg = Config(ollama_endpoint="", ntfy_url="", **config)
    w = FlightWatcher(cfg, Store(":memory:"), _Searcher(prices), _Notifier())
    w.today = lambda: date(2026, 10, 1)
    return w


def _drain(w):
    while not w._queue.empty():
        watch_id, reason = w._queue.get()
        w.check(w.store.get_watch(watch_id), reason)


def test_add_command_creates_watch_and_checks_immediately():
    w = _watcher([3640])
    reply = w.handle_text("我想要更改目的地是台北到東京", "web")
    assert "已新增追蹤 #1 台北→東京" in reply
    _drain(w)
    latest = w.store.latest_price(1)
    assert latest.price == 3640
    messages = [m for m, _ in w.notifier.sent]
    assert any("目前最低價" in m and "NT$3,640" in m for m in messages)


def test_rolling_window_uses_config_days():
    w = _watcher([3640], window_start_days=7, window_end_days=90)
    w.handle_text("台北到東京", "web")
    _drain(w)
    _, start, end = w.searcher.calls[0]
    assert (start, end) == (date(2026, 10, 8), date(2026, 12, 30))


def test_duplicate_add_rechecks_instead_of_duplicating():
    w = _watcher([3640, 3600])
    w.handle_text("台北到東京", "web")
    reply = w.handle_text("台北到東京", "web")
    assert "已經在追蹤" in reply
    assert len(w.store.list_watches()) == 1


def test_scheduled_alerts_only_on_new_low_or_target():
    w = _watcher([4000, 3990, 3700, 3650], new_low_threshold=0.03)
    watch = w.store.add_watch("台北", ["TPE"], "東京", ["NRT"], max_price=3680)
    w.check(watch, "scheduled")           # first check -> "開始追蹤"
    w.check(watch, "scheduled")           # 0.25% lower -> silent
    w.check(w.store.get_watch(watch.id), "scheduled")  # 7.3% lower -> new low
    w.check(w.store.get_watch(watch.id), "scheduled")  # 1.4% lower but crosses target
    heads = [m.splitlines()[0] for m, _ in w.notifier.sent]
    assert len(heads) == 3
    assert "開始追蹤" in heads[0]
    assert "新低價" in heads[1]
    assert "達到目標價" in heads[2]


def test_scheduled_error_is_recorded_not_notified():
    w = _watcher([RuntimeError("rate limited")])
    watch = w.store.add_watch("台北", ["TPE"], "東京", ["NRT"])
    assert w.check(watch, "scheduled") is None
    assert "rate limited" in w.store.get_watch(watch.id).last_error
    assert w.notifier.sent == []


def test_replace_remove_list_and_target():
    w = _watcher()
    w.handle_text("台北到東京", "web")
    w.handle_text("台北到大阪", "web")
    reply = w.handle_text("只追蹤台北到首爾", "web")
    assert "已停止追蹤 #1, #2" in reply
    assert [x.route for x in w.store.list_watches()] == ["台北→首爾"]
    assert "#3 台北→首爾" in w.handle_text("列表", "web")
    assert "NT$4,000" in w.handle_text("#3 目標 4000", "web")
    assert w.store.get_watch(3).max_price == 4000
    assert "已停止追蹤 #3" in w.handle_text("刪除 #3", "web")
    assert w.store.list_watches() == []


def test_due_watches_respects_interval():
    w = _watcher(check_interval_minutes=180)
    watch = w.store.add_watch("台北", ["TPE"], "東京", ["NRT"])
    assert [x.id for x in w.due_watches(time.time())] == [watch.id]
    w.store.update_watch(watch.id, last_checked_at=time.time())
    assert w.due_watches(time.time()) == []
    assert [x.id for x in w.due_watches(time.time() + 181 * 60)] == [watch.id]


def test_metrics_follow_active_watches():
    w = _watcher([3640])
    w.handle_text("台北到東京", "web")
    _drain(w)
    text = METRICS.render()
    assert 'flightwatch_price{codes="TPE→NRT/HND",currency="TWD",route="台北→東京",watch_id="1"} 3640' in text
    w.handle_text("刪除 #1", "web")
    assert "flightwatch_price{" not in METRICS.render()
    assert METRICS.get("flightwatch_watches_active") == 0


def test_listener_skips_bot_messages_and_tracks_since():
    import json

    from flightwatch.listener import NtfyListener

    seen = []
    listener = NtfyListener("http://ntfy", "flights", lambda text, source: seen.append((text, source)))
    assert listener.since == "none"  # ntfy rejects since=now with HTTP 400
    listener._on_line(json.dumps({"event": "keepalive"}).encode())
    listener._on_line(json.dumps({"event": "message", "id": "a1", "message": "台北到東京"}).encode())
    listener._on_line(json.dumps({"event": "message", "id": "a2", "message": "✅ 已新增",
                                  "tags": ["robot"]}).encode())
    assert seen == [("台北到東京", "ntfy")]
    assert listener.since == "a2"


def test_metrics_keep_timestamp_precision_and_zero_counters():
    from flightwatch.metrics import Registry

    reg = Registry()
    reg.set("flightwatch_last_check_timestamp_seconds", 1790793715.25, watch_id=1)
    reg.set("flightwatch_watches_active", 2)
    text = reg.render()
    assert 'flightwatch_last_check_timestamp_seconds{watch_id="1"} 1790793715.25' in text
    assert "flightwatch_watches_active 2\n" in text
    _watcher()
    assert 'flightwatch_commands_total{action="add",parser="llm"} 0' in METRICS.render()


def test_state_has_dashboard_summary():
    from flightwatch.web import state

    w = _watcher([3640])
    w.handle_text("台北到東京", "web")
    assert state(w)["watches"][0]["summary"] == "查詢中…"
    _drain(w)
    assert state(w)["watches"][0]["summary"] == "NT$3,640 · 10/31"


def test_dates_only_follow_up_continues_last_route():
    w = _watcher([3640, 4100])
    w.handle_text("台北到東京", "ntfy")
    reply = w.handle_text("明年的機票", "ntfy")
    assert "明年幾月" in reply and "台北→東京" in reply
    reply = w.handle_text("2027/3月", "web")
    assert "已新增追蹤 #2 台北→東京" in reply
    assert w.store.get_watch(2).date_from == "2027-03-01"


def test_too_far_ahead_is_refused():
    w = _watcher()
    assert "太遠了" in w.handle_text("台北到東京 2028/3月", "web")


def test_web_remove_button_leaves_a_trail():
    import json
    import threading
    import urllib.request

    from flightwatch.web import serve

    w = _watcher()
    w.handle_text("台北到東京", "web")
    server = serve(w, 0)
    port = server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/watches/1/remove", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    reply = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))["reply"]
    server.shutdown()
    assert "網頁按鈕" in reply
    texts = [m["text"] for m in w.store.recent_messages()]
    assert any("停止追蹤" in t and "#1" in t for t in texts)
