"""Live web dashboard: pod status + what the agent diagnosed / fixed / learned.

Stdlib only (http.server). Serves one HTML page that polls /api/state.
Runs in a daemon thread next to the agent loop.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Optional

from .events import workload_of
from .kube import pod_is_ready
from .models import WATCHED_SYMPTOMS, Symptom

if TYPE_CHECKING:
    from .app import Agent

log = logging.getLogger("aiops.dashboard")

_POD_CACHE_SECONDS = 2.0
_ZERO_STAGES = ("remediated", "validated", "validation_failed", "rolled_back", "learned",
                "judge_passed", "judge_rejected")
_SOFT_SYMPTOMS = {Symptom.PENDING, Symptom.CONTAINER_CREATING}


def pod_summary(pod: dict[str, Any], agent: "Agent") -> dict[str, Any]:
    meta = pod.get("metadata", {})
    status = pod.get("status", {})
    spec = pod.get("spec", {})
    phase = status.get("phase", "")
    ready = pod_is_ready(pod)
    cstatuses = status.get("containerStatuses", [])
    by_name = {cs.get("name"): cs for cs in cstatuses}

    issue = agent.collector.analyze(pod)
    if meta.get("deletionTimestamp"):
        level, state = "done", "Terminating"
    elif issue and issue.symptom in WATCHED_SYMPTOMS:
        level = "warn" if issue.symptom in _SOFT_SYMPTOMS else "bad"
        state = issue.symptom.value
    elif ready:
        level, state = "ok", "Running"
    elif phase == "Succeeded":
        level, state = "done", "Completed"
    else:
        level, state = "warn", phase or "Unknown"

    containers = []
    for c in spec.get("containers", []) or [{"name": n} for n in by_name]:
        cs = by_name.get(c.get("name"), {})
        cstate = cs.get("state", {})
        kind = next(iter(cstate), "")
        reason = cstate.get(kind, {}).get("reason", "") if kind else ""
        containers.append(
            {
                "name": c.get("name", ""),
                "image": c.get("image") or cs.get("image", ""),
                "memory_limit": c.get("resources", {}).get("limits", {}).get("memory", ""),
                "state": f"{kind} ({reason})" if reason else kind,
            }
        )

    total = len(containers) or len(cstatuses)
    ready_count = sum(1 for cs in cstatuses if cs.get("ready", ready))
    return {
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "workload": workload_of(pod),
        "created": meta.get("creationTimestamp", ""),
        "phase": phase,
        "state": state,
        "level": level,
        "message": issue.message if issue else "",
        "ready": f"{ready_count}/{total}",
        "restarts": sum(cs.get("restartCount", 0) for cs in cstatuses),
        "containers": containers,
    }


class Dashboard:
    def __init__(self, agent: "Agent", host: str, port: int) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._pods_at = 0.0
        self._pods: list[dict[str, Any]] = []
        self._pods_error: Optional[str] = None
        self._server: Optional[ThreadingHTTPServer] = None

    # --- state -------------------------------------------------------------
    def _pods_snapshot(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        # Fetched live (not from the agent loop) so the view stays current
        # even while the agent blocks on a multi-minute validation.
        with self._lock:
            if time.time() - self._pods_at > _POD_CACHE_SECONDS:
                try:
                    raw = self.agent.collector.list_watched_pods()
                    self._pods = [pod_summary(p, self.agent) for p in raw]
                    self._pods_error = None
                except Exception as exc:
                    self._pods_error = str(exc)
                self._pods_at = time.time()
            return self._pods, self._pods_error

    def state(self) -> dict[str, Any]:
        cfg = self.agent.config
        pods, pods_error = self._pods_snapshot()
        pods = sorted(pods, key=lambda p: (p["level"] == "ok", p["namespace"], p["name"]))

        kb = self.agent.pipeline.kb
        kb_entries = kb.entries() if hasattr(kb, "entries") else []
        return {
            "generated_at": time.time(),
            "agent": {
                "cluster": cfg.cluster_name,
                "namespaces": "all" if cfg.all_namespaces else ", ".join(cfg.namespaces),
                "auto_fix": cfg.auto_fix,
                "dry_run": cfg.dry_run,
                "llm": cfg.ollama_model if cfg.llm_enabled else None,
                "demo": type(self.agent.pipeline.kube).__name__ == "FakeKubeClient",
                "poll_interval": cfg.poll_interval_seconds,
                "last_scan_at": self.agent.last_scan_at,
                "last_scan_error": self.agent.last_scan_error,
            },
            "pods": pods,
            "pods_error": pods_error,
            "events": self.agent.events.recent(200),
            "kb": {
                "total": len(kb_entries),
                "recent": [
                    {"symptom": e.symptom, "solution": e.solution, "timestamp": e.timestamp,
                     "auto": bool(e.patch)}
                    for e in reversed(kb_entries[-5:])
                ],
            },
        }

    def metrics(self) -> str:
        """Prometheus text format: agent activity counters + KB size + scan health."""
        lines = [
            "# HELP aiops_events_total Agent pipeline events by stage, source and namespace",
            "# TYPE aiops_events_total counter",
        ]
        counts = self.agent.events.counts()
        # Publish key series at 0 before they first happen: Prometheus'
        # increase() can't see a counter that is born at 1.
        cfg = self.agent.config
        for namespace in ([] if cfg.all_namespaces else cfg.namespaces):
            for stage in _ZERO_STAGES:
                for source in ("rule", "kb", "llm"):
                    counts.setdefault((stage, source, namespace), 0)
            counts.setdefault(("detected", "", namespace), 0)
            counts.setdefault(("guard_blocked", "web", namespace), 0)
        for (stage, source, namespace), count in sorted(counts.items()):
            lines.append(
                f'aiops_events_total{{stage="{stage}",source="{source}",namespace="{namespace}"}} {count}'
            )
        kb = self.agent.pipeline.kb
        kb_entries = kb.entries() if hasattr(kb, "entries") else []
        lines += [
            "# HELP aiops_kb_entries Verified fixes stored in the knowledge base",
            "# TYPE aiops_kb_entries gauge",
            f"aiops_kb_entries {len(kb_entries)}",
            "# HELP aiops_last_scan_timestamp_seconds Unix time of the last successful scan",
            "# TYPE aiops_last_scan_timestamp_seconds gauge",
            f"aiops_last_scan_timestamp_seconds {self.agent.last_scan_at or 0}",
            "# HELP aiops_scan_ok 1 if the last scan succeeded",
            "# TYPE aiops_scan_ok gauge",
            f"aiops_scan_ok {0 if self.agent.last_scan_error else 1}",
        ]
        return "\n".join(lines) + "\n"

    # --- server ------------------------------------------------------------
    def start(self) -> None:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
                elif path == "/api/state":
                    body = json.dumps(dashboard.state(), ensure_ascii=False).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                elif path == "/healthz":
                    self._send(200, "text/plain; charset=utf-8", b"ok")
                elif path == "/metrics":
                    body = dashboard.metrics().encode("utf-8")
                    self._send(200, "text/plain; version=0.0.4; charset=utf-8", body)
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")

            def _send(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("http: " + fmt, *args)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]  # resolves port 0 in tests
        threading.Thread(
            target=self._server.serve_forever, name="aiops-dashboard", daemon=True
        ).start()
        shown_host = "localhost" if self.host in ("127.0.0.1", "0.0.0.0") else self.host
        log.info("dashboard: http://%s:%s", shown_host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


_PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIOps Dashboard</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1a1d23; --muted: #6b7280; --line: #e5e7eb;
  --ok: #16a34a; --ok-bg: #dcfce7; --bad: #dc2626; --bad-bg: #fee2e2;
  --warn: #d97706; --warn-bg: #fef3c7; --done: #6b7280; --done-bg: #f3f4f6;
  --info: #2563eb; --info-bg: #dbeafe;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --panel: #181b21; --text: #e6e8eb; --muted: #9aa1ab; --line: #2a2f37;
    --ok: #4ade80; --ok-bg: #14301f; --bad: #f87171; --bad-bg: #3a1717;
    --warn: #fbbf24; --warn-bg: #3a2c0f; --done: #9aa1ab; --done-bg: #23272e;
    --info: #60a5fa; --info-bg: #172a45;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between;
  padding: 16px 24px; border-bottom: 1px solid var(--line); background: var(--panel); }
h1 { font-size: 18px; margin: 0; }
h2 { font-size: 13px; margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.sub { color: var(--muted); font-size: 13px; }
.badges { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: var(--done-bg); color: var(--done); }
.badge.on { background: var(--info-bg); color: var(--info); }
.badge.demo { background: var(--warn-bg); color: var(--warn); }
.live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); }
.dot.err { background: var(--bad); }
.dot.pulse { animation: pulse 2s infinite; }
@keyframes pulse { 50% { opacity: .35; } }
main { padding: 20px 24px; display: grid; gap: 20px; grid-template-columns: minmax(0, 1fr) 360px; }
@media (max-width: 960px) { main { grid-template-columns: 1fr; padding: 16px; } header { padding: 12px 16px; } }
.stats { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 600px) { .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.stat .n { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stat .l { color: var(--muted); font-size: 12px; }
.stat.ok .n { color: var(--ok); } .stat.bad .n { color: var(--bad); } .stat.info .n { color: var(--info); }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
select, input { background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 10px; font: inherit; }
input { flex: 1; min-width: 160px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
.card { background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--done);
  border-radius: 10px; padding: 12px 14px; min-width: 0; }
.card.ok { border-left-color: var(--ok); } .card.bad { border-left-color: var(--bad); }
.card.warn { border-left-color: var(--warn); }
.card-top { display: flex; justify-content: space-between; gap: 8px; align-items: start; }
.name { font-weight: 600; word-break: break-all; }
.ns { color: var(--muted); font-size: 12px; }
.pill { font-size: 12px; padding: 1px 8px; border-radius: 999px; white-space: nowrap; }
.pill.ok { background: var(--ok-bg); color: var(--ok); } .pill.bad { background: var(--bad-bg); color: var(--bad); }
.pill.warn { background: var(--warn-bg); color: var(--warn); } .pill.done { background: var(--done-bg); color: var(--done); }
.pill.info { background: var(--info-bg); color: var(--info); }
.meta { display: flex; gap: 14px; color: var(--muted); font-size: 12px; margin: 8px 0; flex-wrap: wrap; }
.meta b { color: var(--text); font-weight: 500; }
.ctr { font-size: 12px; color: var(--muted); word-break: break-all; }
.ctr code { color: var(--text); }
.msg { font-size: 12px; color: var(--bad); margin-top: 6px; word-break: break-word; }
.agent { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--line); font-size: 12px; }
.agent .what { margin-top: 4px; word-break: break-word; }
.agent .none { color: var(--muted); }
.side { display: flex; flex-direction: column; gap: 20px; min-width: 0; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
.tl { list-style: none; margin: 0; padding: 0; max-height: 60vh; overflow-y: auto; }
.tl li { padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
.tl li:last-child { border-bottom: 0; }
.tl .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.tl .t { color: var(--muted); font-variant-numeric: tabular-nums; }
.tl .w { font-weight: 600; }
.tl .d { color: var(--muted); margin-top: 2px; word-break: break-word; }
.kb li { padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 12px; list-style: none; }
.kb ul { margin: 0; padding: 0; }
.banner { grid-column: 1 / -1; background: var(--bad-bg); color: var(--bad); border-radius: 10px;
  padding: 10px 14px; font-size: 13px; word-break: break-word; }
.empty { color: var(--muted); padding: 24px; text-align: center; background: var(--panel);
  border: 1px dashed var(--line); border-radius: 10px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>AIOps Dashboard</h1>
    <div class="sub" id="sub">連線中…</div>
  </div>
  <div class="badges">
    <span id="badges"></span>
    <span class="live"><span class="dot pulse" id="dot"></span><span id="live">—</span></span>
  </div>
</header>
<main>
  <div class="banner" id="banner" hidden></div>
  <section class="stats">
    <div class="stat"><div class="n" id="s-total">–</div><div class="l">Pods</div></div>
    <div class="stat ok"><div class="n" id="s-ok">–</div><div class="l">正常 Running / Ready</div></div>
    <div class="stat bad"><div class="n" id="s-bad">–</div><div class="l">異常</div></div>
    <div class="stat info"><div class="n" id="s-fixed">–</div><div class="l">本次已自動修復</div></div>
  </section>
  <section>
    <div class="toolbar">
      <select id="ns"><option value="">全部 namespace</option></select>
      <select id="lvl">
        <option value="">全部狀態</option><option value="bad">只看異常</option><option value="ok">只看正常</option>
      </select>
      <input id="q" placeholder="搜尋 Pod 名稱…" autocomplete="off">
    </div>
    <div class="grid" id="pods"></div>
  </section>
  <aside class="side">
    <div class="panel"><h2>Agent 事件時間軸</h2><ul class="tl" id="events"></ul></div>
    <div class="panel kb"><h2>知識庫 <span id="kb-total"></span></h2><ul id="kb"></ul></div>
  </aside>
</main>
<script>
const STAGES = {
  detected: ["偵測到異常", "bad"], diagnosed: ["完成診斷", "info"], recommended: ["建議（需人工）", "warn"],
  remediated: ["已套用修復", "info"], validated: ["驗證通過", "ok"], learned: ["已學習", "ok"],
  remediation_failed: ["修復失敗", "bad"], validation_failed: ["驗證失敗", "bad"],
  no_diagnosis: ["無法診斷", "bad"], error: ["錯誤", "bad"],
};
const SOURCES = { rule: "規則", kb: "知識庫", llm: "LLM" };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function ago(t) {
  if (!t) return "";
  const ms = typeof t === "number" ? t * 1000 : Date.parse(t);
  if (isNaN(ms)) return "";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return s + " 秒前";
  if (s < 3600) return Math.floor(s / 60) + " 分鐘前";
  if (s < 86400) return Math.floor(s / 3600) + " 小時前";
  return Math.floor(s / 86400) + " 天前";
}
function clock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false });
}
function stagePill(stage) {
  const [label, cls] = STAGES[stage] || [stage, "done"];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

let last = null;

function render() {
  const st = last;
  if (!st) return;
  const a = st.agent;
  $("sub").textContent = `cluster: ${a.cluster} · namespace: ${a.namespaces} · 每 ${a.poll_interval}s 掃描`;
  const b = [];
  if (a.demo) b.push(`<span class="badge demo">DEMO</span>`);
  b.push(`<span class="badge ${a.auto_fix ? "on" : ""}">auto-fix ${a.auto_fix ? "開" : "關"}</span>`);
  if (a.dry_run) b.push(`<span class="badge on">dry-run</span>`);
  b.push(`<span class="badge ${a.llm ? "on" : ""}">LLM ${a.llm ? esc(a.llm) : "關"}</span>`);
  $("badges").innerHTML = b.join(" ");

  const err = st.pods_error || a.last_scan_error;
  $("banner").hidden = !err;
  if (err) $("banner").textContent = "無法取得叢集資料：" + err;
  $("dot").className = "dot pulse" + (err ? " err" : "");
  $("live").textContent = a.last_scan_at ? "上次掃描 " + ago(a.last_scan_at) : "等待第一次掃描";

  const pods = st.pods;
  $("s-total").textContent = pods.length;
  $("s-ok").textContent = pods.filter((p) => p.level === "ok").length;
  $("s-bad").textContent = pods.filter((p) => p.level === "bad" || p.level === "warn").length;
  $("s-fixed").textContent = st.events.filter((e) => e.stage === "learned").length;

  const nsSel = $("ns");
  const cur = nsSel.value;
  const nss = [...new Set(pods.map((p) => p.namespace))].sort();
  nsSel.innerHTML = `<option value="">全部 namespace</option>` +
    nss.map((n) => `<option ${n === cur ? "selected" : ""}>${esc(n)}</option>`).join("");

  // Latest agent event per workload (pods get new names after a rollout).
  const latest = {};
  for (const e of st.events) {
    const k = e.namespace + "/" + e.workload;
    if (!latest[k]) latest[k] = e;
  }

  const q = $("q").value.trim().toLowerCase();
  const lvl = $("lvl").value;
  const shown = pods.filter((p) =>
    (!nsSel.value || p.namespace === nsSel.value) &&
    (!q || p.name.toLowerCase().includes(q)) &&
    (!lvl || (lvl === "ok" ? p.level === "ok" : p.level !== "ok")));

  $("pods").innerHTML = shown.length ? shown.map((p) => {
    const e = latest[p.namespace + "/" + p.workload];
    const agent = e
      ? `<div class="row">${stagePill(e.stage)} <span class="ns">${esc(SOURCES[e.source] || "")} · ${esc(ago(e.ts))}</span></div>
         <div class="what">${esc(e.detail)}</div>`
      : `<span class="none">Agent 尚未處理</span>`;
    const ctrs = p.containers.map((c) =>
      `<div class="ctr">${esc(c.name)}: <code>${esc(c.image || "—")}</code>` +
      (c.memory_limit ? ` · mem <code>${esc(c.memory_limit)}</code>` : "") +
      (c.state ? ` · ${esc(c.state)}` : "") + `</div>`).join("");
    const pill = p.level === "ok" ? "ok" : p.level;
    return `<article class="card ${esc(p.level)}">
      <div class="card-top">
        <div><div class="name">${esc(p.name)}</div><div class="ns">${esc(p.namespace)} · ${esc(p.workload)}</div></div>
        <span class="pill ${esc(pill)}">${esc(p.state)}</span>
      </div>
      <div class="meta"><span>Ready <b>${esc(p.ready)}</b></span><span>Restarts <b>${esc(p.restarts)}</b></span>
        ${p.created ? `<span>建立 <b>${esc(ago(p.created))}</b></span>` : ""}</div>
      ${ctrs}
      ${p.message && p.level !== "ok" ? `<div class="msg">${esc(p.message)}</div>` : ""}
      <div class="agent">${agent}</div>
    </article>`;
  }).join("") : `<div class="empty">${pods.length ? "沒有符合篩選的 Pod" : "目前沒有 Pod"}</div>`;

  $("events").innerHTML = st.events.length ? st.events.map((e) => `<li>
      <div class="row"><span class="t">${esc(clock(e.ts))}</span>${stagePill(e.stage)}<span class="w">${esc(e.workload || e.pod)}</span></div>
      <div class="d">${esc(e.detail)}</div></li>`).join("")
    : `<li class="d">Agent 還沒有處理任何事件</li>`;

  $("kb-total").textContent = `（${st.kb.total} 筆）`;
  $("kb").innerHTML = st.kb.recent.map((k) => `<li><span class="pill ${k.auto ? "ok" : "done"}">${esc(k.symptom)}</span>
      ${esc(k.solution)} <span class="ns">${esc(ago(k.timestamp))}</span></li>`).join("") ||
    `<li class="d">尚無紀錄</li>`;
}

async function poll() {
  try {
    const r = await fetch("api/state", { cache: "no-store" });
    last = await r.json();
    render();
  } catch (err) {
    $("dot").className = "dot err";
    $("live").textContent = "與 Agent 失去連線";
  }
}
["ns", "lvl", "q"].forEach((id) => $(id).addEventListener("input", render));
poll();
setInterval(poll, 3000);
</script>
</body>
</html>
"""
