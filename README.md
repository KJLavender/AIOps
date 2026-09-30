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
`ContainerCreating`, `Pending`, `Failed`.

## Built-in rules

| Symptom | Action | Auto-fix |
| --- | --- | --- |
| `OOMKilled` | raise memory limit, restart Deployment | ✅ patch |
| `ImagePullBackOff` / `ErrImagePull` | check tag & registry | advisory → KB |
| `CrashLoopBackOff` | collect logs/events, forward to KB/LLM | advisory → KB/LLM |

> Safety: only **structured patches** (rules, or KB entries carrying a patch) are
> applied automatically. Free-text LLM suggestions are **recommendations only**.

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
```

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
| `AIOPS_VALIDATION_TIMEOUT` | `300` | rollout wait (s) |
| `AIOPS_LLM_ENABLED` | `false` | enable Ollama fallback |
| `AIOPS_OLLAMA_ENDPOINT` | `http://localhost:11434` | Ollama URL |
| `AIOPS_OLLAMA_MODEL` | `llama3` | model name |
| `AIOPS_KB_PATH` | `data/knowledge_base.jsonl` | KB file |

## HomeLab PoC — fault simulation

```powershell
kubectl apply -f manifests/00-namespace.yaml
kubectl apply -f manifests/case2-oomkilled.yaml   # OOMKilled → auto memory bump
kubectl apply -f manifests/case3-imagepull.yaml   # ImagePullBackOff → advisory
kubectl apply -f manifests/case1-crashloop.yaml   # CrashLoopBackOff → KB/LLM
kubectl apply -f manifests/case4-missing-env.yaml # Missing ENV → CrashLoop

python -m aiops
```

Case 2 (OOMKilled) is the end-to-end demo: the agent detects the OOM, patches the
Deployment memory limit, waits for rollout, confirms Running/Ready, and records a
verified KB entry.

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
