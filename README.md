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
Knowledge Base     (verified structured records)
   ↓ (miss)
LLM analysis       (only when rule + KB both miss; off by default)
   ↓
Auto Remediation   (kubectl patch — structured patches only)
   ↓
Validation         (Running & Ready & stable for 5 min)
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
`AIOPS_DASHBOARD_PORT` / `--port` (default `8080`).

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

## Tests

```powershell
pip install pytest
pytest -q
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

Phase 1 (this repo): Rule Engine + KB + validation + learning.
Later: vector KB (Qdrant/Chroma/Elasticsearch), and split into a Multi-Agent
platform — Monitoring / Diagnosis / Repair / Validation / Knowledge agents.
