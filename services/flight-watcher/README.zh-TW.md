# 機票追蹤（Flight watcher）

[English](README.md) | **繁體中文**

追蹤機票價格，降價時通知你。想改追蹤內容，只要打一句話——在網頁介面或 ntfy 的 `flights` topic 都可以。

```
你：我想要更改目的地是台北到東京
🤖：✅ 已新增追蹤 #1 台北→東京（TPE→NRT/HND）… 正在查詢目前最低價
🤖：🔎 #1 目前最低價  台北→東京  NT$3,260  10/27（二）
    Peach Aviation MM628 直飛 17:10
```

- **資料**：透過 [`fli`](https://github.com/punitarani/fli) 取得 Google Flights 資料
  （逆向工程的 API，不需要 key）。每次檢查先找出該航線日期範圍內最便宜的那一天，
  再列出那天最便宜的航班。價格以新台幣計。
- **排程**：每條航線每 `CHECK_INTERVAL_MINUTES`（180）分鐘重新檢查一次，新指令會立刻查詢。
  只有出現新低價（比前一次低點再低 ≥ 3%）或跌破目標價時才會通知。
- **指令**：由 Ollama（`qwen3.5:4b`）把自由文字轉成指令；確定性的解析器負責常見寫法，
  LLM 掛掉或給出查不到的地名時由它接手。解析器找到的日期、價格、旅程天數優先於 LLM 的判讀。

| 你輸入 | 效果 |
| --- | --- |
| `台北到東京` / `改成去大阪` | 新增航線（預設出發地台北）；滾動區間為 7–90 天後 |
| `高雄飛大阪 12月 來回5天 5000以下 直飛` | 月份區間、來回 5 天、目標 NT$5,000、直飛 |
| `台北到首爾 12/20-12/28` | 固定日期區間 |
| `只追蹤台北到首爾` | 用這條取代所有航線 |
| `列表` · `立即查詢` · `刪除 #2` · `#1 目標 4000` · `說明` | 列出 · 立刻查 · 停止追蹤 · 設定目標價 · 說明 |

## Endpoints

`/` 網頁介面 · `/api/state` · `POST /api/command {"text": "..."}` ·
`/metrics`（Prometheus：`flightwatch_price`、`flightwatch_checks_total`…） · `/healthz`

## 部署（k3s）

```bash
docker build -t flight-watcher:local services/flight-watcher
docker save flight-watcher:local | sudo k3s ctr images import -
kubectl apply -f services/flight-watcher/k8s/
```

`k8s/` 裡有 `travel` namespace、ntfy（自架，`ntfy.localhost:8000`）
和追蹤服務本身（`flights.localhost:8000`），各自附 ServiceMonitor；
Grafana 的 **Flight Deals** 儀表板讀的就是這些 metrics。出門在外也想收通知的話，
把 `EXTRA_NOTIFY_URLS` 設成公開的 topic，例如 `https://ntfy.sh/<很長的隨機名稱>`，
再在 ntfy app 裡訂閱它。

僅限低頻率的個人使用：資料來源是非官方的 Google API，檢查間隔請放寬一點。
