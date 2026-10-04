# GitHub digest

**English** | [繁體中文](README.zh-TW.md)

One image, two daily digests, each running in its own pod (`MODE`):

| Pod | When | What |
| --- | --- | --- |
| `github-daily` (`MODE=daily-top`) | 09:00 | Repos **created in the last 24 h** with the most stars (Top 10), each with a one-line zh-TW summary |
| `repo-picks` (`MODE=research-picks`) | 09:05 | 5 repos matching your research topics that were **never recommended before**, each with "why it matters for your research" |

- **Data**: GitHub search API (no key needed; 10 requests/min unauthenticated,
  so picks run takes ~1 min). Set `GITHUB_TOKEN` to lift the limit.
- **Bait filter**: brand-new repos at the top of "stars today" are often malware
  lures with bought stars, so names/descriptions like *cheat, crack, activator,
  loader…* and day-old repos with no code language are skipped.
- **Picks**: per topic, one "rising" search (created ≤ 180 days, ≥ 10★) and one
  "established" search (pushed ≤ 60 days, ≥ 50★); results are taken
  round-robin across topics so no single topic fills the list.
- **Summaries**: Ollama (`qwen3.5:4b`); repos without a description get
  "（作者沒有寫描述）" instead of an invented purpose.
- **Catch-up**: if the pod starts after today's run time and today's digest is
  missing, it runs immediately.

## Commands (web page or ntfy topic)

| Type | Effect |
| --- | --- |
| `立即整理` / `立即推薦` | run now (replaces today's list) |
| `新增主題 eBPF` | add a research topic (any GitHub search words) |
| `主題` | list topics |
| `刪除主題 #2` | remove a topic |

Default topics: `aiops`, `kubernetes self-healing`, `llm sre agent`,
`kubernetes llm`, `observability opentelemetry`.

## Endpoints

`/` page (`?day=YYYY-MM-DD` for history) · `/api/state` · `POST /api/command` ·
`/metrics` (`ghdigest_runs_total`, `ghdigest_last_run_timestamp_seconds`,
`ghdigest_items`, `ghdigest_topics`) · `/healthz`

## Deploy

```bash
docker build -t github-digest:local services/github-digest
docker save github-digest:local | sudo k3s ctr images import -
kubectl apply -f services/github-digest/k8s/
```
