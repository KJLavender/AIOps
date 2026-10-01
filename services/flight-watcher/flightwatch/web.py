"""Web UI + JSON API + Prometheus metrics (stdlib http.server)."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .metrics import METRICS
from .service import FlightWatcher

log = logging.getLogger("flightwatch.web")

_WATCH_ACTION_RE = re.compile(r"^/api/watches/(\d+)/(check|remove)$")


def state(watcher: FlightWatcher) -> dict:
    watches = []
    for w in watcher.store.list_watches():
        history = watcher.store.history(w.id, limit=120)
        latest = history[-1] if history else None
        watches.append({
            "id": w.id,
            "route": w.route,
            "codes": w.codes_label,
            "window": w.describe_window(),
            "max_price": w.max_price,
            "nonstop": w.nonstop,
            "last_checked_at": w.last_checked_at,
            "last_error": w.last_error,
            "lowest": watcher.store.lowest_price(w.id),
            "latest": latest.__dict__ if latest else None,
            # One readable line for dashboards (e.g. the Homepage portal).
            "summary": (f"{watcher._money(latest.price, latest.currency)} · {latest.travel_date[5:].replace('-', '/')}"
                        if latest else "查詢中…"),
            "history": [{"t": p.checked_at, "price": p.price, "date": p.travel_date} for p in history],
        })
    return {
        "watches": watches,
        "messages": watcher.store.recent_messages(25),
        "interval_minutes": watcher.config.check_interval_minutes,
        "last_cycle_at": watcher.last_cycle_at,
        "now": time.time(),
    }


def make_handler(watcher: FlightWatcher):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # route access logs through logging, quietly
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, state(watcher))
            elif path == "/metrics":
                self._send(200, METRICS.render().encode("utf-8"), "text/plain; version=0.0.4")
            elif path == "/healthz":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            if path == "/api/command":
                reply = watcher.handle_text(str(body.get("text", "")), "web")
                return self._json(200, {"reply": reply})
            m = _WATCH_ACTION_RE.match(path)
            if m:
                watch = watcher.store.get_watch(int(m.group(1)))
                if not watch or not watch.active:
                    return self._json(404, {"error": "no such watch"})
                if m.group(2) == "check":
                    watcher.request_check(watch.id, "manual")
                    return self._json(200, {"reply": f"🔄 正在查詢 #{watch.id} {watch.route}"})
                watcher.store.update_watch(watch.id, active=False)
                watcher.refresh_metrics()
                return self._json(200, {"reply": f"🗑️ 已停止追蹤 #{watch.id} {watch.route}"})
            self._json(404, {"error": "not found"})

    return Handler


def serve(watcher: FlightWatcher, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(watcher))
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web UI on :%s", port)
    return server


_PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>機票追蹤</title>
<style>
:root {
  color-scheme: light;
  --surface-0: #f4f3f0; --surface-1: #fcfcfb; --border: #e4e2dc;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #6f6d68;
  --series-1: #2a78d6; --grid: #ebe9e4;
  --good: #0ca30c; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface-0: #121211; --surface-1: #1a1a19; --border: #2e2e2b;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #9a998f;
    --series-1: #3987e5; --grid: #2a2a27;
  }
}
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 system-ui, "Noto Sans TC", "Microsoft JhengHei", sans-serif;
       background: var(--surface-0); color: var(--text-primary); }
main { max-width: 1080px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--text-secondary); margin: 0 0 20px; font-size: 13px; }
form { display: flex; gap: 8px; margin-bottom: 8px; }
input[type=text] { flex: 1; padding: 10px 12px; font: inherit; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); }
button { padding: 8px 14px; font: inherit; border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface-1); color: var(--text-primary); cursor: pointer; }
button.primary { background: var(--series-1); border-color: var(--series-1); color: #fff; }
.reply { white-space: pre-wrap; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px; margin: 0 0 20px; min-height: 1em; color: var(--text-secondary); }
.reply:empty { display: none; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.card h2 { font-size: 16px; margin: 0; }
.meta { color: var(--text-muted); font-size: 12px; }
.hero { font-size: 28px; font-weight: 650; margin: 8px 0 0; font-variant-numeric: tabular-nums; }
.detail { color: var(--text-secondary); font-size: 13px; }
.badge { display: inline-flex; gap: 4px; align-items: center; font-size: 12px; color: var(--text-primary); }
.badge i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.spark { position: relative; margin-top: 10px; }
.spark svg { display: block; width: 100%; height: 64px; overflow: visible; }
.tip { position: absolute; top: -6px; transform: translate(-50%, -100%); pointer-events: none;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px;
  font-size: 12px; white-space: nowrap; display: none; color: var(--text-primary); }
.actions { display: flex; gap: 6px; margin-top: 10px; }
.actions button { font-size: 13px; padding: 4px 10px; }
.log { margin-top: 28px; }
.log h3 { font-size: 15px; margin: 0 0 8px; }
.log li { list-style: none; padding: 6px 0; border-bottom: 1px solid var(--border); white-space: pre-wrap; font-size: 13px; }
.log ul { padding: 0; margin: 0; }
.who { color: var(--text-muted); font-size: 12px; margin-right: 6px; }
.empty { color: var(--text-secondary); }
</style>
</head>
<body>
<main>
  <h1>✈️ 機票追蹤</h1>
  <p class="sub" id="sub">載入中…</p>
  <form id="f">
    <input id="q" type="text" autocomplete="off"
           placeholder="例如：台北到東京 ／ 高雄飛大阪 12月 5000以下 ／ 列表 ／ 刪除 #2">
    <button class="primary" type="submit">送出</button>
  </form>
  <div class="reply" id="reply"></div>
  <div class="grid" id="cards"></div>
  <section class="log">
    <h3>對話紀錄（也會同步到 ntfy）</h3>
    <ul id="log"></ul>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money = (v, cur) => v == null ? "—" : (cur && cur !== "TWD" ? cur + " " : "NT$") + Math.round(v).toLocaleString();
const ago = (t) => { if (!t) return "尚未檢查"; const m = Math.round((Date.now()/1000 - t) / 60);
  return m < 1 ? "剛剛" : m < 60 ? m + " 分鐘前" : Math.round(m / 60) + " 小時前"; };

function spark(w) {
  const h = w.history;
  if (h.length < 2) return '<div class="meta">價格走勢會在第二次檢查後出現</div>';
  const W = 300, H = 64, pad = 6;
  const ps = h.map((p) => p.price), lo = Math.min(...ps), hi = Math.max(...ps);
  // Show at least a ±10% band so a NT$1 wobble doesn't look like a surge.
  const span = Math.max(hi - lo, hi * 0.2), base = (hi + lo) / 2 - span / 2;
  const x = (i) => pad + i * (W - 2 * pad) / (h.length - 1);
  const y = (v) => H - pad - (v - base) * (H - 2 * pad) / span;
  const pts = h.map((p, i) => `${x(i).toFixed(1)},${y(p.price).toFixed(1)}`).join(" ");
  const last = h.length - 1;
  return `<div class="spark" data-id="${w.id}">
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="${esc(w.route)} 價格走勢，最低 ${money(lo)}，最高 ${money(hi)}">
      <line x1="${pad}" x2="${W - pad}" y1="${y(lo)}" y2="${y(lo)}" stroke="var(--grid)" stroke-width="1"/>
      <polyline points="${pts}" fill="none" stroke="var(--series-1)" stroke-width="2"
                stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      <circle cx="${x(last)}" cy="${y(h[last].price)}" r="4" fill="var(--series-1)"
              stroke="var(--surface-1)" stroke-width="2"/>
      <line class="xh" y1="0" y2="${H}" stroke="var(--text-muted)" stroke-width="1" visibility="hidden"
            vector-effect="non-scaling-stroke"/>
    </svg><div class="tip"></div></div>`;
}

function bindSpark(el, w) {
  const svg = el.querySelector("svg"), tip = el.querySelector(".tip"), xh = el.querySelector(".xh");
  const h = w.history, W = 300, pad = 6;
  svg.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(h.length - 1, Math.round(((e.clientX - r.left) / r.width * W - pad) / (W - 2 * pad) * (h.length - 1))));
    const px = pad + i * (W - 2 * pad) / (h.length - 1);
    xh.setAttribute("x1", px); xh.setAttribute("x2", px); xh.setAttribute("visibility", "visible");
    const d = new Date(h[i].t * 1000);
    tip.textContent = `${money(h[i].price)} · 出發 ${h[i].date} · ${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:00 查到`;
    tip.style.left = (px / W * 100) + "%"; tip.style.display = "block";
  });
  svg.addEventListener("mouseleave", () => { tip.style.display = "none"; xh.setAttribute("visibility", "hidden"); });
}

async function post(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json(); $("reply").textContent = j.reply || j.error || ""; refresh();
}

async function refresh() {
  const st = await (await fetch("/api/state")).json();
  $("sub").textContent = `每 ${Math.round(st.interval_minutes / 60 * 10) / 10} 小時自動檢查 · 追蹤 ${st.watches.length} 條航線 · 也可以在 ntfy 的 flights 頻道直接打字`;
  $("cards").innerHTML = st.watches.length ? st.watches.map((w) => {
    const l = w.latest, hit = l && w.max_price && l.price <= w.max_price;
    const status = w.last_error ? `<span class="badge"><i style="background:var(--critical)"></i>查詢失敗</span>`
      : hit ? `<span class="badge"><i style="background:var(--good)"></i>已達目標價</span>` : "";
    return `<div class="card">
      <h2>#${w.id} ${esc(w.route)} <span class="meta">${esc(w.codes)}</span></h2>
      <div class="meta">${esc(w.window)}${w.max_price ? " · 目標 " + money(w.max_price) : ""}${w.nonstop ? " · 直飛" : ""} · ${ago(w.last_checked_at)}</div>
      <div class="hero">${l ? money(l.price, l.currency) : "查詢中…"}</div>
      <div class="detail">${l ? `${esc(l.travel_date)}${l.return_date ? " → " + esc(l.return_date) : ""} ${esc(l.airline || "")} ${esc(l.flight_no || "")} ${l.depart_time ? esc(l.depart_time) : ""}` : ""}
        ${w.lowest != null ? " · 歷史最低 " + money(w.lowest, l && l.currency) : ""} ${status}</div>
      ${spark(w)}
      <div class="actions"><button data-act="check" data-id="${w.id}">立即查詢</button>
        <button data-act="remove" data-id="${w.id}">停止追蹤</button></div>
    </div>`; }).join("") : '<p class="empty">還沒有追蹤任何航線，在上面輸入「台北到東京」試試看。</p>';
  st.watches.forEach((w) => { const el = document.querySelector(`.spark[data-id="${w.id}"]`); if (el) bindSpark(el, w); });
  $("log").innerHTML = st.messages.map((m) => `<li><span class="who">${m.direction === "in" ? "你（" + esc(m.source) + "）" : "🤖"} ${new Date(m.ts * 1000).toLocaleString()}</span>${esc(m.text)}</li>`).join("");
}

$("f").addEventListener("submit", (e) => { e.preventDefault(); const t = $("q").value.trim(); if (!t) return;
  $("reply").textContent = "處理中…"; $("q").value = ""; post("/api/command", {text: t}); });
$("cards").addEventListener("click", (e) => { const b = e.target.closest("button[data-act]"); if (!b) return;
  post(`/api/watches/${b.dataset.id}/${b.dataset.act}`); });
refresh(); setInterval(refresh, 15000);
</script>
</body>
</html>
"""
