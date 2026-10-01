# AI Ops Agent for Kubernetes

A self-healing agent that watches Kubernetes Pods, diagnoses failures with a
**Rule Engine first** (no LLM tokens wasted), falls back to a **Knowledge Base**
and optionally an **LLM**, auto-remediates, validates, and **learns** verified
fixes.

```
Kubernetes → Detect → Diagnose → Fix → Validate → Learn
```

## Pipeline (MVP)

```
Pod fails
   ↓
Rule Engine        (known symptoms → structured fix)
   ↓ (miss / defer)
Collect Logs + Events
   ↓
Knowledge Base     (same workload + symptom first, then keyword match)
   ↓ (miss)
Web search         (Exa — same backend as the agent-reach skill; optional)
   ↓
LLM analysis       (logs + events + spec + web results; may name ONE fix from
   ↓                a bounded catalog, otherwise recommendation only)
Auto Remediation   (kubectl patch — structured patches only)
   ↓
Validation         (Running & Ready & stable) ──fail──► rollout undo
   ↓
Learning           (store only verified + validated cases)
```

## Watched symptoms

`CrashLoopBackOff`, `ImagePullBackOff`, `ErrImagePull`, `OOMKilled`,
`ContainerCreating`, `CreateContainerConfigError`, `NotReady` (Running but not
Ready past a grace period), `Pending`, `Failed`.

## Built-in rules

| Symptom | Action | Auto-fix |
| --- | --- | --- |
| `OOMKilled` | raise memory limit, restart Deployment | ✅ patch |
| `ImagePullBackOff` / `ErrImagePull` | tag not found + repo has an approved fallback → swap image; else check tag & registry | ✅ allowlisted / advisory → KB/LLM |
| `CrashLoopBackOff` + logs name a missing env var | var has an approved value → set it; else precise advisory | ✅ allowlisted / advisory → KB |
| `CrashLoopBackOff` (other) | collect logs/events, forward to KB/LLM | advisory → KB/LLM |
| `CreateContainerConfigError` | name the missing ConfigMap / Secret | advisory |
| `Pending` | explain scheduling failure (resources, selector, taint, PVC) | advisory |
| `NotReady` | surface the failing readiness probe, forward to KB/LLM | advisory → KB/LLM |

> Safety: only **structured patches** (rules, or KB entries carrying a patch) are
> applied automatically. The agent never guesses an image or env value — only
> operator-approved ones (`AIOPS_IMAGE_FALLBACKS`, `AIOPS_ENV_DEFAULTS`). A
> learned image/env patch only auto-applies to the Deployment it was verified on.
> Free-text LLM suggestions are **recommendations only**, and ones below
> `AIOPS_LLM_MIN_CONFIDENCE` are dropped.

### Web search + LLM fixes (`AIOPS_WEB_SEARCH`, `AIOPS_LLM_AUTO_FIX`)

For failures no rule or KB entry solves, the agent searches the web for the
error (cluster IPs and pod hashes stripped), hands the results to the LLM as
*untrusted* context, and lets it name one action from `aiops/actions.py`:

| Symptom | Allowed actions | Guard rails |
| --- | --- | --- |
| `NotReady` | `set_readiness_probe_path`, `rollout_restart` | path must be a plain URL path, differ from the failing one, **and answer 2xx/3xx on the pod before it is applied** |
| `Pending` | `lower_requests` | may only lower existing requests |
| `OOMKilled` | `set_memory_limit` | must raise the limit, capped by `AIOPS_MAX_MEMORY` |

The LLM never writes a patch; the action builds it from the live Deployment.
Anything else it suggests (new image, privileged sidecar, ...) has no action to
map onto and stays a recommendation. LLM confidence must reach
`AIOPS_LLM_AUTO_FIX_MIN_CONFIDENCE`, a fix that fails validation is rolled back
(`kubectl rollout undo`) and never retried, and a validated fix is learned so
the next occurrence on that workload is fixed straight from the KB.

## Requirements

- Python 3.10+
- `kubectl` on PATH, configured against your cluster (k3s works great for HomeLab)
- (optional) [Ollama](https://ollama.com) for the LLM layer

```powershell
pip install -r requirements.txt
```

## Run

```powershell
# One scan (safe: no changes applied)
python -m aiops --once --dry-run

# Recommendations only, keep watching
python -m aiops --no-auto-fix

# Full self-healing loop
python -m aiops

# Self-healing loop + live web dashboard at http://localhost:8080
python -m aiops --dashboard

# Dashboard against the built-in fake cluster (no kubectl needed)
python -m aiops --demo --dashboard
```

### Dashboard

`--dashboard` serves a stdlib-only web page (poll every 3s) showing each Pod's
status, container image / memory limit, the agent's latest action per workload,
an event timeline (detected → diagnosed → remediated → validated → learned) and
recent Knowledge Base entries. JSON is available at `/api/state`.
Bind address / port: `AIOPS_DASHBOARD_HOST` (default `127.0.0.1`),
`AIOPS_DASHBOARD_PORT` / `--port` (default `8080`). Prometheus metrics
(`aiops_events_total{stage,source,namespace}`, `aiops_kb_entries`, scan health)
are served at `/metrics`.

### Configuration (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| `AIOPS_ALL_NAMESPACES` | `true` | watch every namespace |
| `AIOPS_NAMESPACES` | `default` | comma list (when not all namespaces) |
| `AIOPS_POLL_INTERVAL` | `15` | scan interval (s) |
| `AIOPS_AUTO_FIX` | `true` | apply structured patches |
| `AIOPS_DRY_RUN` | `false` | diagnose without patching |
| `AIOPS_DEFAULT_MEMORY` | `512Mi` | floor for OOM memory bump |
| `AIOPS_MEMORY_SCALE` | `2.0` | multiply current limit on OOM |
| `AIOPS_IMAGE_FALLBACKS` | _(empty)_ | approved images, `repo=image,...` (e.g. `nginx=nginx:1.27-alpine`) |
| `AIOPS_ENV_DEFAULTS` | _(empty)_ | approved env values, `VAR=value,...` |
| `AIOPS_NOT_READY_GRACE` | `120` | seconds NotReady before it counts as an issue |
| `AIOPS_VALIDATION_TIMEOUT` | `300` | rollout wait (s) |
| `AIOPS_LLM_ENABLED` | `false` | enable Ollama fallback |
| `AIOPS_OLLAMA_ENDPOINT` | `http://localhost:11434` | Ollama URL |
| `AIOPS_OLLAMA_MODEL` | `llama3` | model name |
| `AIOPS_LLM_MIN_CONFIDENCE` | `0.6` | drop LLM answers below this confidence |
| `AIOPS_LLM_NUM_CTX` | `8192` | Ollama context window (the default silently truncates long prompts) |
| `AIOPS_WEB_SEARCH` | `false` | search the web for unknown failures |
| `AIOPS_EXA_ENDPOINT` | `https://mcp.exa.ai/mcp` | Exa MCP endpoint (no key needed) |
| `AIOPS_LLM_AUTO_FIX` | `false` | let the LLM pick a catalog action |
| `AIOPS_LLM_AUTO_FIX_MIN_CONFIDENCE` | `0.7` | confidence needed to apply it |
| `AIOPS_MAX_MEMORY` | `2Gi` | cap for LLM-proposed memory raises |
| `AIOPS_ACTION_PRECHECK` | `true` | probe a proposed readiness path on the pod first |
| `AIOPS_ROLLBACK` | `true` | `rollout undo` when validation fails |
| `AIOPS_PENDING_GRACE` | `60` | seconds Pending / ContainerCreating before it counts |
| `AIOPS_KB_PATH` | `data/knowledge_base.jsonl` | KB file |

## HomeLab PoC — fault simulation

```powershell
kubectl apply -f manifests/00-namespace.yaml
kubectl apply -f manifests/case2-oomkilled.yaml   # OOMKilled → auto memory bump
kubectl apply -f manifests/case3-imagepull.yaml   # ImagePullBackOff → auto image swap (if allowlisted)
kubectl apply -f manifests/case1-crashloop.yaml   # CrashLoopBackOff → KB/LLM
kubectl apply -f manifests/case4-missing-env.yaml # Missing ENV → auto env fix (if allowlisted)
kubectl apply -f manifests/case5-pending.yaml     # Pending (Insufficient memory) → advisory
kubectl apply -f manifests/case6-readiness.yaml   # NotReady (bad readiness probe) → KB/LLM
kubectl apply -f manifests/case7-missing-configmap.yaml # CreateContainerConfigError → advisory

python -m aiops
```

Cases 2, 3 and 4 run end-to-end (detect → patch → rollout → Running/Ready →
verified KB entry); 3 and 4 need the allowlists that `deploy/agent-deployment.yaml`
sets. In-cluster, the KB lives on the `aiops-kb` PVC (`deploy/agent-pvc.yaml`) so
learned fixes survive pod restarts.

## HomeLab stack: monitoring + a real service

Everything below runs on the same k3s node; see each folder for details.

| URL (from Windows) | What |
| --- | --- |
| **http://localhost:8000** | **Homepage portal — everything below on one page** (live fares, agent stats, pod status) |
| **http://home.100-115-153-20.sslip.io:8000** | the same portal **from a phone** on Tailscale (see below) |
| http://grafana.localhost:8000 | Grafana — stock Kubernetes dashboards + **AIOps Agent** + **Flight Deals** |
| http://flights.localhost:8000 | Flight watcher web UI (type commands in plain Chinese) |
| http://ntfy.localhost:8000/flights | ntfy — push notifications + the same chat channel |
| http://localhost:30080 | The agent's own live dashboard |

- **`monitoring/`** — installed by the k3s helm-controller (`HelmChart` objects,
  no helm CLI needed): kube-prometheus-stack (Prometheus, Grafana,
  node-exporter, kube-state-metrics), Loki in Monolithic mode on a local-path
  PVC (7-day retention), and Grafana Alloy shipping every pod's logs plus
  Kubernetes events to Loki. `00-traefik-hostnetwork.yaml` runs Traefik on the
  host network because WSL2 mirrored networking only forwards Windows
  localhost to real listening sockets. Dashboards are generated by
  `monitoring/dashboards/generate.py`.
- **`portal/homepage.yaml`** — [Homepage](https://gethomepage.dev) as the single
  entry point: each tile links to a service and shows its pods' status plus a
  live widget (watched fares from the flight API, agent metrics from
  Prometheus, Grafana/Prometheus stats).
- **Phone access** — Tailscale runs inside WSL (node `homelab`, 100.115.153.20).
  Every Ingress also answers on `<name>.100-115-153-20.sslip.io` (public DNS
  that resolves to the Tailscale IP), so a phone on the same tailnet opens the
  portal and every service; add the portal to the home screen for an app-like
  icon. The PC can't reach its own WSL Tailscale address, so the portal's
  `custom.js` rewrites links to `*.localhost` when opened locally.
- **Resource isolation** — `cluster/resource-policies.yaml`: every container has
  requests/limits, LimitRange defaults per namespace, and PriorityClasses
  (platform > services > fault cases).
- **`services/flight-watcher/`** — checks Google Flights every 3 h for each
  watched route and notifies via ntfy on a new low or when a target price is
  hit. Change what it watches by typing, e.g. `台北到東京`,
  `高雄飛大阪 12月 來回5天 5000以下`, `列表`, `刪除 #2`, in the web UI or the
  ntfy `flights` topic. The AIOps agent watches this namespace too.

```powershell
kubectl -n monitoring create secret generic grafana-admin `
  --from-literal=admin-user=admin --from-literal=admin-password=<password>
kubectl apply -f monitoring/ ; kubectl apply -f monitoring/dashboards/
kubectl apply -f services/flight-watcher/k8s/
kubectl apply -f deploy/
kubectl apply -f portal/
kubectl apply -f cluster/
```

## Tests

```powershell
pip install pytest
pytest -q        # agent + services/flight-watcher tests
```

## Knowledge Base format

Structured records only — never free-form Q&A:

```json
{
  "symptom": "OOMKilled",
  "root_cause": "memory limit too low",
  "solution": "increase memory to 512Mi",
  "verified": true,
  "timestamp": "...",
  "cluster": "homelab"
}
```

## Roadmap

Phase 1 (this repo): Rule Engine + KB + validation + learning, web-search-assisted
LLM fixes from a bounded catalog with rollback, Prometheus/Loki/Grafana monitoring.
Later: vector KB (Qdrant/Chroma/Elasticsearch), and split into a Multi-Agent
platform — Monitoring / Diagnosis / Repair / Validation / Knowledge agents.
