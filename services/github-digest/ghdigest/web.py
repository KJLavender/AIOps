"""Web page + JSON API + /metrics."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .notify import METRICS
from .service import DigestService

log = logging.getLogger("ghdigest.web")


def state(svc: DigestService, day: str = "") -> dict:
    digests = svc.store.recent_digests(14)
    current = next((d for d in digests if d["day"] == day), digests[0] if digests else None)
    items = current["items"] if current else []
    for i, item in enumerate(items, 1):
        # One readable line for dashboards (Homepage list widget).
        item["rank_name"] = f"{i}. {item['full_name']}"
        item["stars_text"] = f"★{item['stars']:,}"
    return {
        "mode": svc.config.mode,
        "day": current["day"] if current else None,
        "items": items,
        "days": [d["day"] for d in digests],
        "topics": [{"id": i, "query": q} for i, q in svc.store.topics()],
        "run_at": svc.config.run_at,
        "last_error": svc.last_error,
    }


def serve(svc: DigestService, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._json(state(svc, parse_qs(url.query).get("day", [""])[0]))
            elif url.path == "/metrics":
                self._send(200, METRICS.render().encode("utf-8"), "text/plain; version=0.0.4")
            elif url.path == "/healthz":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if urlparse(self.path).path != "/api/command":
                return self._json({"error": "not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._json({"error": "invalid JSON"}, 400)
            self._json({"reply": svc.handle_text(str(body.get("text", "")), "web")})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web UI on :%s", port)
    return server


_PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub 摘要</title>
<style>
:root { color-scheme: light; --bg: #f4f3f0; --card: #fcfcfb; --border: #e4e2dc;
  --text: #0b0b0b; --text2: #52514e; --muted: #6f6d68; --accent: #2a78d6; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --bg: #121211; --card: #1a1a19;
  --border: #2e2e2b; --text: #fff; --text2: #c3c2b7; --muted: #9a998f; --accent: #3987e5; } }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 system-ui, "Noto Sans TC", "Microsoft JhengHei", sans-serif;
  background: var(--bg); color: var(--text); }
main { max-width: 860px; margin: 0 auto; padding: 20px 14px 40px; }
h1 { font-size: 21px; margin: 0 0 4px; } .sub { color: var(--text2); font-size: 13px; margin: 0 0 14px; }
.days { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
.days a { font-size: 12px; padding: 3px 9px; border-radius: 999px; border: 1px solid var(--border);
  color: var(--text2); text-decoration: none; } .days a.on { background: var(--accent); color: #fff; border-color: var(--accent); }
ol { list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }
li { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; }
.top { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.name { font-weight: 650; color: var(--text); text-decoration: none; word-break: break-all; }
.stars { font-variant-numeric: tabular-nums; color: var(--text); white-space: nowrap; }
.meta { color: var(--muted); font-size: 12px; } .sum { margin: 4px 0 2px; } .desc { color: var(--text2); font-size: 13px; }
form { display: flex; gap: 8px; margin: 16px 0 8px; } input { flex: 1; padding: 9px 11px; font: inherit;
  border-radius: 8px; border: 1px solid var(--border); background: var(--card); color: var(--text); }
button { padding: 8px 14px; font: inherit; border-radius: 8px; border: 0; background: var(--accent); color: #fff; }
.reply { white-space: pre-wrap; color: var(--text2); font-size: 13px; } .topics { font-size: 13px; color: var(--text2); }
.empty { color: var(--text2); }
</style></head><body><main>
<h1 id="title">GitHub 摘要</h1><p class="sub" id="sub"></p>
<div class="days" id="days"></div>
<ol id="list"></ol>
<form id="f"><input id="q" placeholder="例如：立即整理 ／ 新增主題 eBPF ／ 主題 ／ 刪除主題 #2"><button>送出</button></form>
<div class="reply" id="reply"></div><p class="topics" id="topics"></p>
</main><script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let day = new URLSearchParams(location.search).get("day") || "";
async function load() {
  const st = await (await fetch("/api/state" + (day ? "?day=" + day : ""))).json();
  const picks = st.mode === "research-picks";
  $("title").textContent = picks ? "🧭 今日研究推薦" : "🔥 GitHub 今日新星 Top 10";
  $("sub").textContent = (picks ? "依你的研究主題，每天推薦沒推薦過的 repo" : "過去 24 小時新建立、星數最高的 repo")
    + ` · 每天 ${st.run_at} 更新` + (st.last_error ? ` · 上次失敗：${st.last_error}` : "");
  $("days").innerHTML = st.days.map((d) => `<a href="?day=${d}" class="${d === st.day ? "on" : ""}">${d.slice(5).replace("-", "/")}</a>`).join("");
  $("list").innerHTML = st.items.length ? st.items.map((r, i) => `<li><div class="top">
      <a class="name" href="${esc(r.url)}" target="_blank" rel="noopener">${i + 1}. ${esc(r.full_name)}</a>
      <span class="stars">★ ${r.stars.toLocaleString()}</span></div>
      <div class="sum">${esc(r.summary)}</div>
      ${r.description ? `<div class="desc">${esc(r.description)}</div>` : ""}
      <div class="meta">${esc(r.language || "")}${r.topics.length ? " · " + esc(r.topics.slice(0, 5).join(", ")) : ""}</div></li>`).join("")
    : '<p class="empty">還沒有資料，輸入「立即整理」或「立即推薦」。</p>';
  $("topics").textContent = picks ? "目前主題：" + st.topics.map((t) => `#${t.id} ${t.query}`).join("、") : "";
}
$("f").addEventListener("submit", async (e) => { e.preventDefault(); const t = $("q").value.trim(); if (!t) return;
  $("q").value = ""; const r = await fetch("/api/command", {method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({text: t})}); $("reply").textContent = (await r.json()).reply; load(); });
load(); setInterval(load, 30000);
</script></body></html>
"""
