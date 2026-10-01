# Flight watcher

Watches flight prices and tells you when they drop. You change what it watches
by typing a sentence — in the web UI or in the ntfy `flights` topic.

```
你：我想要更改目的地是台北到東京
🤖：✅ 已新增追蹤 #1 台北→東京（TPE→NRT/HND）… 正在查詢目前最低價
🤖：🔎 #1 目前最低價  台北→東京  NT$3,260  10/27（二）
    Peach Aviation MM628 直飛 17:10
```

- **Data**: Google Flights through [`fli`](https://github.com/punitarani/fli)
  (reverse-engineered API, no key). Each check asks for the cheapest day in the
  route's date window, then names that day's cheapest flight. Prices in TWD.
- **Schedule**: every route is re-checked every `CHECK_INTERVAL_MINUTES`
  (180). New commands are checked immediately. Alerts go out only for a new
  low (≥ 3 % below the previous low) or when a target price is crossed.
- **Commands**: Ollama (`qwen2.5:7b`) turns free text into a command; a
  deterministic parser handles the common forms and takes over when the LLM is
  down or names a place that doesn't resolve. Dates/prices/trip length found by
  the parser win over the LLM's reading.

| You type | Effect |
| --- | --- |
| `台北到東京` / `改成去大阪` | add a route (default origin 台北); rolling window 7–90 days ahead |
| `高雄飛大阪 12月 來回5天 5000以下 直飛` | month window, 5-day round trip, target NT$5,000, non-stop |
| `台北到首爾 12/20-12/28` | fixed date range |
| `只追蹤台北到首爾` | replace all routes with this one |
| `列表` · `立即查詢` · `刪除 #2` · `#1 目標 4000` · `說明` | list · check now · stop · set target · help |

## Endpoints

`/` web UI · `/api/state` · `POST /api/command {"text": "..."}` ·
`/metrics` (Prometheus: `flightwatch_price`, `flightwatch_checks_total`, …) · `/healthz`

## Deploy (k3s)

```bash
docker build -t flight-watcher:local services/flight-watcher
docker save flight-watcher:local | sudo k3s ctr images import -
kubectl apply -f services/flight-watcher/k8s/
```

`k8s/` holds the `travel` namespace, ntfy (self-hosted, `ntfy.localhost:8000`)
and the watcher (`flights.localhost:8000`), each with a ServiceMonitor; the
Grafana **Flight Deals** dashboard reads those metrics. For notifications away
from home, set `EXTRA_NOTIFY_URLS` to a public topic such as
`https://ntfy.sh/<long-random-name>` and subscribe to it in the ntfy app.

Low-volume personal use only: the data source is an unofficial Google API, so
keep the interval generous.
