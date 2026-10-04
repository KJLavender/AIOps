# GitHub 每日精選（GitHub digest）

[English](README.md) | **繁體中文**

一個 image、兩份每日精選，各自跑在自己的 pod 裡（由 `MODE` 決定）：

| Pod | 時間 | 內容 |
| --- | --- | --- |
| `github-daily`（`MODE=daily-top`） | 09:00 | **過去 24 小時內建立**、星數最多的 repo（前 10 名），每個附一行繁中摘要 |
| `repo-picks`（`MODE=research-picks`） | 09:05 | 5 個符合你研究主題、而且**從沒推薦過**的 repo，每個附上「對你的研究為什麼重要」 |

- **資料**：GitHub search API（不需要 key；未驗證身分每分鐘 10 次請求，
  所以精選要跑約 1 分鐘）。設定 `GITHUB_TOKEN` 可以解除限制。
- **誘餌過濾**：衝上「今日星數」榜的全新 repo 常常是買星的惡意誘餌，
  所以名稱或描述含 *cheat、crack、activator、loader…* 的，以及沒有程式語言、剛建一天的 repo 都會跳過。
- **精選**：每個主題各做一次「新興」搜尋（建立 ≤ 180 天、≥ 10★）和一次「成熟」搜尋
  （最近 push ≤ 60 天、≥ 50★）；結果在各主題之間輪流取，避免單一主題佔滿整份清單。
- **摘要**：用 Ollama（`qwen3.5:4b`）產生；沒有描述的 repo 會寫「（作者沒有寫描述）」，
  不會自己編一個用途。
- **補跑**：如果 pod 在今天的排程時間之後才啟動，而今天的精選還沒產生，就會立刻補跑。

## 指令（網頁或 ntfy topic）

| 輸入 | 效果 |
| --- | --- |
| `立即整理` / `立即推薦` | 立刻執行（取代今天的清單） |
| `新增主題 eBPF` | 新增研究主題（任何 GitHub 搜尋關鍵字都行） |
| `主題` | 列出主題 |
| `刪除主題 #2` | 刪除主題 |

預設主題：`aiops`、`kubernetes self-healing`、`llm sre agent`、
`kubernetes llm`、`observability opentelemetry`。

## Endpoints

`/` 頁面（加 `?day=YYYY-MM-DD` 看歷史紀錄） · `/api/state` · `POST /api/command` ·
`/metrics`（`ghdigest_runs_total`、`ghdigest_last_run_timestamp_seconds`、
`ghdigest_items`、`ghdigest_topics`） · `/healthz`

## 部署

```bash
docker build -t github-digest:local services/github-digest
docker save github-digest:local | sudo k3s ctr images import -
kubectl apply -f services/github-digest/k8s/
```
