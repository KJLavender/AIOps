# AIOps 工作區說明文件

> 專案：**AI Ops Agent for Kubernetes**（`aiops` v0.1.0）
> 文件日期：2026-10-01
> 狀態：Phase 1 MVP，測試 17/17 通過（Python 3.11）

---

## 1. 專案概述

這是一個 **Kubernetes 自我修復代理（self-healing agent）**。它會定期掃描叢集中的 Pod，偵測異常狀態，然後依序嘗試：

1. **Rule Engine（規則引擎）**：已知症狀直接給出結構化修復方案，不消耗 LLM token。
2. **Knowledge Base（知識庫）**：以關鍵字比對過去「已驗證」的修復紀錄。
3. **LLM（Ollama）**：規則與知識庫都查不到時才呼叫，預設關閉。

找到可自動套用的修復方案後，代理會用 `kubectl patch` 修改 Deployment，等待 rollout 完成並確認 Pod 持續 Running & Ready，最後把成功的修復寫回知識庫（學習）。

```
Kubernetes → Detect → Diagnose → Fix → Validate → Learn
```

**設計重點**

- **純標準函式庫**：執行時期沒有任何第三方套件（`requirements.txt` 是空的），只依賴 `kubectl` 執行檔。
- **安全優先**：只有「結構化 patch」（來自規則或帶有 patch 的 KB 紀錄）會被自動套用；LLM 的自由文字建議一律只當作建議，不會自動執行。
- **只學習驗證過的案例**：修復成功且驗證通過才會寫入知識庫。

---

## 2. 目錄結構

```
AIOps/
├── aiops/                     # 主程式套件
│   ├── __init__.py            # 版本號 0.1.0
│   ├── __main__.py            # CLI 入口：python -m aiops
│   ├── app.py                 # 組裝元件、Agent 主迴圈（tick / run_forever / cooldown）
│   ├── config.py              # Config dataclass，環境變數覆寫
│   ├── models.py              # 資料模型：Symptom, PodIssue, Diagnosis, KBEntry…
│   ├── collector.py           # 掃描 Pod → 產生 PodIssue
│   ├── kube.py                # kubectl 的薄包裝（subprocess）
│   ├── pipeline.py            # 核心流程：Rule → KB → LLM → 修復 → 驗證 → 學習
│   ├── remediation.py         # 套用 patch（支援 dry-run）
│   ├── validation.py          # rollout 狀態 + Ready + 穩定期檢查
│   ├── learning.py            # 驗證成功後寫入 KB
│   ├── patches.py             # strategic-merge patch 產生器（記憶體 / image / env）
│   ├── util.py                # 記憶體單位解析、分詞
│   ├── fakes.py               # 假叢集（--demo 模式用，免 kubectl）
│   ├── events.py              # Agent 事件時間軸（記憶體 ring buffer，供儀表板讀取）
│   ├── dashboard.py           # 即時網頁儀表板（http.server，/、/api/state、/metrics）
│   ├── websearch.py           # Exa 網路搜尋（MCP），結果只當 LLM 的參考資料
│   ├── actions.py             # LLM 可選的白名單修復動作 + 參數檢查
│   ├── rules/
│   │   ├── base.py            # Rule 基底類別、RuleContext
│   │   ├── builtin.py         # 內建七條規則
│   │   └── engine.py          # RuleEngine：第一條符合的規則勝出
│   ├── knowledge_base/
│   │   ├── base.py            # KnowledgeBase 抽象介面
│   │   └── json_store.py      # JSONL 實作 + Jaccard 關鍵字評分
│   └── llm/
│       ├── base.py            # LLMAnalyzer 介面、LLMInput、NullAnalyzer
│       └── ollama.py          # Ollama /api/generate 實作
├── data/
│   └── knowledge_base.jsonl   # 知識庫資料（目前 3 筆 OOMKilled 紀錄）
├── manifests/                 # 故障模擬用的 K8s 資源（7 個案例）
├── deploy/                    # 代理本身部署到叢集的資源（RBAC、PVC、Deployment、metrics、Ollama 端點）
├── monitoring/                # Prometheus / Grafana / Loki / Alloy（k3s HelmChart）+ 儀表板
├── services/flight-watcher/   # 機票追蹤服務（程式、Dockerfile、k8s、測試）
├── tests/                     # pytest 單元測試
├── Dockerfile                 # python:3.12-slim + kubectl v1.31.0
├── .dockerignore / .gitignore
├── requirements.txt           # 無執行期依賴；開發用 pytest
└── README.md                  # 原始英文說明
```

> 已是 git repository（遠端 `github.com/KJLavender/AIOps`，直接推 `main`）。

---

## 3. 架構與處理流程

### 3.1 主迴圈（`app.py`）

`Agent.run_forever()` 每 `poll_interval_seconds`（預設 15 秒）呼叫一次 `tick()`：

1. `Collector.scan()` 取得所有異常 `PodIssue`。
2. 每個 issue 以 `namespace/pod/symptom` 為 key，**600 秒內不重複處理**（cooldown，避免反覆修同一個問題）。
3. 交給 `Pipeline.handle(issue)`；單一 issue 發生例外不會中斷迴圈。

### 3.2 異常偵測（`collector.py`）

依以下優先順序判斷每個 container（含 init container）：

| 順序 | 條件 | 判定症狀 |
| --- | --- | --- |
| 1 | `lastState.terminated.reason == OOMKilled` | `OOMKilled` |
| 2 | `state.terminated.reason == OOMKilled` | `OOMKilled` |
| 3 | `state.waiting.reason` 為 CrashLoopBackOff / ImagePullBackOff / ErrImagePull / ContainerCreating / CreateContainerConfigError | 對應症狀 |
| 4 | `state.terminated` 非 0 exit code 且 `restartCount >= 1` | `CrashLoopBackOff`（抓重啟間隔中的 Error 狀態） |
| 5 | Pod `Running`、container 在跑但 `ready=false` 超過 `not_ready_grace_seconds`（預設 120 秒） | `NotReady` |
| 6 | Pod `phase == Pending` | `Pending` |
| 7 | Pod `phase == Failed` | `Failed` |

### 3.3 Pipeline（`pipeline.py`）

```
PodIssue
   │
   ▼
_build_context  ── 只有 CrashLoopBackOff / Failed / NotReady 才抓 logs、previous logs、events、describe
   │
   ▼
RuleEngine.diagnose（規則可以回傳 None 讓給下一條規則）
   │
   ├─ 沒有規則符合，或規則標記 forward_to_kb 且不可自動修復
   │        ▼
   │   KB.find_for_target（同一個 Deployment + 同症狀、驗證過的 patch）──命中──► Diagnosis(source=KB)
   │        │ 未命中
   │   KB.search（關鍵字 Jaccard）──命中──► Diagnosis(source=KB)
   │        │ 未命中
   │        ▼
   │   WebSearch（AIOPS_WEB_SEARCH，Exa；查詢去掉 IP / pod hash）
   │        ▼
   │   LLM.analyze（llm_enabled 且規則的 consult_llm=True 時；網路結果標為「不可信參考」）
   │        ──► confidence 低於門檻則丟棄
   │        ──► 若 LLM 指定了白名單 action（actions.py）且通過檢查 → 變成可自動套用的 patch
   │
   │   規則已精確判斷（consult_llm=False）時，只有「可自動套用」的 KB 結果能取代它
   │
   ▼
沒有 diagnosis → 留給人工處理
有 diagnosis 但不可自動修復 → 只記錄建議
auto_fix 關閉 → 略過修復
   │
   ▼
Remediator.remediate  (kubectl patch)
   │
   ▼
Validator.validate    (rollout status → 等 Ready → 穩定期)
   │                   失敗 → kubectl rollout undo，並記住這個修法不再重試
   ▼
LearningEngine.record (寫入 KB，verified=true，帶 target_name)
```

---

## 4. 核心資料模型（`models.py`）

| 類別 | 用途 | 重要欄位 |
| --- | --- | --- |
| `Symptom` (Enum) | 監看的症狀，值與 kubectl reason 字串一致 | CrashLoopBackOff, ImagePullBackOff, ErrImagePull, OOMKilled, ContainerCreating, CreateContainerConfigError, NotReady, Pending, Failed, Unknown |
| `PodIssue` | 單一 Pod/container 的異常 | namespace, pod, container, symptom, message, restart_count；`key` 屬性 |
| `Diagnosis` | 診斷結果 | source (rule/kb/llm), root_cause, summary, actions, confidence, **patch**, target_*, **auto_fixable**, **forward_to_kb**, **consult_llm** |
| `RemediationResult` | 修復結果 | attempted, success, detail, patch |
| `ValidationResult` | 驗證結果 | success, detail |
| `KBEntry` | 知識庫紀錄（結構化，非 Q&A） | symptom, problem, root_cause, solution, verified, confidence, timestamp, cluster, patch, target_kind, **target_name** |

`auto_fixable=True` 且帶有 `patch` 的 Diagnosis 才會被自動套用。

---

## 5. 規則引擎（`rules/`）

`RuleEngine` 依序檢查規則，**第一條符合且回傳診斷的規則勝出**（規則可回傳 None 讓給下一條）。內建規則：

| 規則 | 症狀 | 行為 | 自動修復 |
| --- | --- | --- | --- |
| `OOMKilledRule` | OOMKilled | 透過 ownerReferences 找到 Deployment，計算新記憶體上限並產生 patch | ✅（有 Deployment 時） |
| `ImagePullRule` | ImagePullBackOff / ErrImagePull | registry 回報 tag 不存在，且該 repo 在 `AIOPS_IMAGE_FALLBACKS` 有核准的替代 image → 換 image；否則建議檢查 tag/registry，轉交 KB/LLM | ✅（白名單內）/ ❌ 建議 |
| `MissingEnvRule` | CrashLoopBackOff | 從 logs 抓出缺少的環境變數（`X is not set`、`KeyError: 'X'`、`environment variable X`…）；變數在 `AIOPS_ENV_DEFAULTS` 有核准值 → 補上 env；否則給精確建議（不問 LLM） | ✅（白名單內）/ ❌ 建議 |
| `CrashLoopBackOffRule` | CrashLoopBackOff | 收集 logs/events，轉交 KB / LLM | ❌ 建議 |
| `ConfigRefRule` | CreateContainerConfigError | 指出缺少哪個 ConfigMap / Secret | ❌ 建議（不捏造設定資料） |
| `PendingRule` | Pending | 解析排程訊息：資源不足、nodeSelector/affinity、taint、PVC 未綁定；無法辨識則讓給 KB/LLM | ❌ 建議 |
| `NotReadyRule` | NotReady | 從 events 抓**最新一筆** readiness probe 失敗訊息，轉交 KB / 網路搜尋 / LLM | 見第 7 節 |

**白名單原則**：代理從不「猜」正確的 image 或環境變數值。只有維運人員事先核准的值才會自動套用，其餘一律只給建議。

**OOM 記憶體計算**：`新上限 = max(目前上限 × memory_scale_factor, default_memory_limit)`

- 64Mi → 128Mi，但低於 512Mi 下限 → **512Mi**
- 512Mi → **1Gi**
- 沒有設定上限 → 512Mi
- Pod 沒有所屬 Deployment（裸 Pod）→ 只給建議

產生的 patch 會同時設定 `limits.memory` 與 `requests.memory`。

`ContainerCreating`、`Failed` 沒有對應規則，直接進入 KB / LLM 流程。新建立的 Pod 會短暫處於 Pending / ContainerCreating，超過 `AIOPS_PENDING_GRACE`（預設 60 秒）才算異常。

---

## 6. 知識庫（`knowledge_base/`）

- **介面**：`KnowledgeBase`（`search` / `add`），保留日後替換為向量資料庫（Qdrant / Chroma / Elasticsearch / Milvus）的空間。
- **Phase 1 實作**：`JsonlKnowledgeBase`
  - 儲存：append-only JSONL（`data/knowledge_base.jsonl`）
  - 搜尋條件：`verified == true` 且 `symptom` 完全相同
  - 評分：查詢文字（症狀 + 訊息 + logs + events）與紀錄文字的 **Jaccard 相似度**
  - 門檻：`kb_score_threshold`（預設 0.35）
- **KB 命中的自動修復**：該筆紀錄帶有 `patch`、能找到 owner Deployment，**且 `target_name` 與目前的 Deployment 相同**（或為 None 的舊紀錄）時才會自動套用。image / env 這類與 workload 綁定的 patch 因此不會被套到別的 Deployment 上。
- **叢集內持久化**：`deploy/agent-pvc.yaml` 建立 `aiops-kb` PVC（local-path），掛在 `/kb`；init container 在第一次啟動時把映像裡的 `data/knowledge_base.jsonl` 複製過去當種子。Pod 重建後學到的紀錄仍在。

`data/knowledge_base.jsonl`（種子資料）：3 筆 OOMKilled 紀錄（1 筆 `patch: null`，2 筆為 case2 實際修復後學到的紀錄，含 512Mi patch）。

---

## 7. LLM 層（`llm/`）

- 預設使用 `NullAnalyzer`（永遠回傳 None）。
- `AIOPS_LLM_ENABLED=true` 時改用 `OllamaAnalyzer`：
  - 呼叫 `POST {endpoint}/api/generate`，`format: "json"`，`num_ctx`（預設 8192）、`temperature: 0`
  - **一定要設 `num_ctx`**：Ollama 預設 context 會默默砍掉長 prompt 的開頭（也就是指令），模型只會回一段 describe 的 JSON、confidence 0
  - Prompt 包含：症狀、訊息、events / logs（保留尾端最新的部分）、describe、Deployment spec、網路搜尋結果；結尾再重申一次輸出格式
  - 要求回傳 `{"root_cause", "fix", "confidence", "action", "params"}`；confidence > 1 會自動除以 100
  - confidence 低於 `llm_min_confidence`（預設 0.6）或沒有 fix 的回答**直接丟棄**，保留規則的診斷
  - 自由文字永遠不會被套用；只有 `action` 欄位指定的白名單動作有機會（見下）

### 網路搜尋 + LLM 自動修復（`websearch.py`、`actions.py`）

規則和 KB 都解決不了時：

1. **搜尋**：用症狀 + 最具體的錯誤行（去掉 IP、pod hash）呼叫 Exa（`https://mcp.exa.ai/mcp`，和 Claude Code 的 agent-reach skill 同一個搜尋後端，免 key）。要帶自訂 `User-Agent`，預設的 `Python-urllib` 會被擋（403）。
2. **LLM 選動作**：搜尋結果當作「不可信參考資料」放進 prompt。LLM 只能從白名單挑一個動作：

| 症狀 | 允許的動作 | 防護 |
| --- | --- | --- |
| NotReady | `set_readiness_probe_path`、`rollout_restart` | 路徑格式檢查、不能是正在失敗的那個路徑，**套用前先直接打 Pod 確認新路徑回 2xx/3xx** |
| Pending | `lower_requests` | 只能調低既有的 requests |
| OOMKilled | `set_memory_limit` | 只能調高，上限 `AIOPS_MAX_MEMORY`（2Gi） |

   CrashLoopBackOff 刻意不在清單裡：kubelet 本來就會重啟，而正確的 image / env 值猜不得。
3. **套用條件**：`AIOPS_LLM_AUTO_FIX=true`、confidence ≥ `AIOPS_LLM_AUTO_FIX_MIN_CONFIDENCE`（0.7）、動作符合症狀、參數通過檢查、這個修法之前沒失敗過。patch 由動作依照當下的 Deployment 產生，LLM 從頭到尾不寫 patch，所以網頁內容就算想誘導「換成某個 image」也沒有對應動作可用。
4. **驗證失敗 → `kubectl rollout undo`**，並記住不再重試；成功則學進 KB，下次同一個 workload 出同樣問題直接用 KB 修，不必再搜尋。

實測（case6，nginx 的 readiness probe 打 `/healthz` 回 404）：搜尋 → LLM 選 `set_readiness_probe_path` `/index.html` → 套用 → Running & Ready → 學習；再弄壞一次時直接 `KB exact hit` 修好。

---

## 8. 修復與驗證

### 修復（`remediation.py`）

- 使用 `kubectl patch <kind> <name> -n <ns> --type strategic -p <json>`。
- `dry_run=True` 時只記錄「would patch」，不實際修改叢集。
- `patches.py` 提供三種 patch 產生器：`memory_limit_patch`（OOMKilledRule）、`image_patch`（ImagePullRule）、`env_patch`（MissingEnvRule）。
- `Remediator.rollback` 在驗證失敗時執行 `kubectl rollout undo`（`AIOPS_ROLLBACK`）。

### 驗證（`validation.py`）

1. `kubectl rollout status deployment/<name> --timeout=<validation_timeout_seconds>s`（subprocess 的逾時多留 30 秒，否則 kubectl 會先被砍掉、跳過 rollback）
2. 以 Deployment 的 `matchLabels` 當 selector，等待所有（非終止中的）Pod 都 Running & Ready
3. **穩定期**：在 `validation_stability_seconds`（預設 60 秒）內持續輪詢，期間任何 Pod NotReady 即判定失敗

---

## 9. 設定（環境變數）

全部定義在 `config.py` 的 `Config.from_env()`。README 只列了一部分，完整清單如下：

| 環境變數 | 預設值 | 說明 |
| --- | --- | --- |
| `AIOPS_ALL_NAMESPACES` | `true` | 監看所有 namespace |
| `AIOPS_NAMESPACES` | `default` | 逗號分隔的 namespace 清單（非全域時） |
| `AIOPS_POLL_INTERVAL` | `15` | 掃描間隔（秒） |
| `AIOPS_KUBECTL_BIN` | `kubectl` | kubectl 執行檔路徑 |
| `AIOPS_AUTO_FIX` | `true` | 是否自動套用結構化 patch |
| `AIOPS_DRY_RUN` | `false` | 只診斷不修改 |
| `AIOPS_DEFAULT_MEMORY` | `512Mi` | OOM 修復的記憶體下限 |
| `AIOPS_MEMORY_SCALE` | `2.0` | OOM 時目前上限的放大倍數 |
| `AIOPS_COOLDOWN` | `600` | 同一 issue 重複處理的冷卻時間（秒） |
| `AIOPS_IMAGE_FALLBACKS` | （空） | 核准的替代 image，`repo=image` 逗號分隔，例如 `nginx=nginx:1.27-alpine` |
| `AIOPS_ENV_DEFAULTS` | （空） | 核准的環境變數值，`VAR=value` 逗號分隔，例如 `DB_HOST=db.local` |
| `AIOPS_NOT_READY_GRACE` | `120` | Running 但 NotReady 超過幾秒才視為異常 |
| `AIOPS_VALIDATION_TIMEOUT` | `300` | rollout / Ready 等待上限（秒） |
| `AIOPS_VALIDATION_POLL` | `15` | 驗證輪詢間隔（秒） |
| `AIOPS_STABILITY` | `60` | 穩定期長度（秒） |
| `AIOPS_KB_PATH` | `data/knowledge_base.jsonl` | 知識庫檔案 |
| `AIOPS_KB_THRESHOLD` | `0.35` | KB 相似度門檻 |
| `AIOPS_CLUSTER` | `homelab` | 寫入 KB 紀錄的叢集名稱 |
| `AIOPS_LLM_ENABLED` | `false` | 啟用 Ollama |
| `AIOPS_OLLAMA_ENDPOINT` | `http://localhost:11434` | Ollama URL |
| `AIOPS_OLLAMA_MODEL` | `llama3` | 模型名稱 |
| `AIOPS_LLM_TIMEOUT` | `60` | LLM 請求逾時（秒） |
| `AIOPS_LLM_MIN_CONFIDENCE` | `0.6` | 低於此 confidence 的 LLM 回答會被丟棄 |
| `AIOPS_LLM_NUM_CTX` | `8192` | Ollama context 長度 |
| `AIOPS_WEB_SEARCH` | `false` | 未知故障時上網搜尋 |
| `AIOPS_EXA_ENDPOINT` | `https://mcp.exa.ai/mcp` | Exa MCP 端點 |
| `AIOPS_WEB_SEARCH_RESULTS` / `_MAX_CHARS` / `_TIMEOUT` | `3` / `4000` / `30` | 搜尋結果數、截斷長度、逾時 |
| `AIOPS_LLM_AUTO_FIX` | `false` | 允許 LLM 從白名單挑動作自動修復 |
| `AIOPS_LLM_AUTO_FIX_MIN_CONFIDENCE` | `0.7` | 套用所需的 confidence |
| `AIOPS_MAX_MEMORY` | `2Gi` | LLM 調高記憶體的上限 |
| `AIOPS_ACTION_PRECHECK` | `true` | 套用新 probe 路徑前先打 Pod 驗證 |
| `AIOPS_ROLLBACK` | `true` | 驗證失敗時 rollout undo |
| `AIOPS_PENDING_GRACE` | `60` | Pending / ContainerCreating 超過幾秒才算異常 |
| `AIOPS_LOG_TAIL` | `200` | 抓取 log 的行數 |
| `AIOPS_DASHBOARD_HOST` | `127.0.0.1` | 儀表板綁定位址（叢集內用 `0.0.0.0`） |
| `AIOPS_DASHBOARD_PORT` | `8080` | 儀表板 port（可用 `--port` 覆寫） |

---

## 10. 執行方式

```powershell
# 不需要 kubectl：用內建假叢集跑一次完整流程（KB 寫到暫存檔，不影響 data/）
python -m aiops --demo

# 即時網頁儀表板 http://localhost:8080（可加 --demo 免叢集試用）
python -m aiops --dashboard
python -m aiops --demo --dashboard

# 掃描一次，不修改叢集
python -m aiops --once --dry-run

# 持續監看，只給建議
python -m aiops --no-auto-fix

# 完整自我修復迴圈
python -m aiops
```

| CLI 參數 | 作用 |
| --- | --- |
| `--once` | 只掃描一次後結束 |
| `--dry-run` | 診斷但不套用 patch |
| `--no-auto-fix` | 只輸出建議 |
| `--demo` | 使用 `fakes.FakeKubeClient`，驗證等待時間設為 0，KB 寫到系統暫存資料夾的 `aiops-demo-kb.jsonl` |
| `--dashboard` | 啟動網頁儀表板並持續監看；搭配 `--demo` 時第一次掃描延後 10 秒，方便先看到異常狀態 |
| `--port` | 儀表板 port |
| `--log-level` | 預設 `INFO` |

### 儀表板內容

- **統計列**：Pod 總數、正常、異常、本次已自動修復數量
- **Pod 卡片**：狀態顏色、Ready、重啟次數、image、記憶體上限、異常訊息，以及 Agent 對該 workload 的最新動作（依 workload 對應，所以 rollout 換了新 Pod 名稱也接得上）
- **事件時間軸**：偵測 → 診斷 → 修復 → 驗證 → 學習，各步驟的時間與結果
- **知識庫**：總筆數與最近 5 筆紀錄
- 支援 namespace / 狀態篩選、名稱搜尋，深淺色主題自動切換；每 3 秒更新

Pod 狀態是儀表板每次請求時直接向叢集查詢（快取 2 秒），所以 Agent 在等待驗證時畫面仍會即時更新。Agent 事件只存在記憶體，重啟後清空；長期趨勢看 Grafana（`/metrics` 提供 `aiops_events_total{stage,source,namespace}`、`aiops_kb_entries`、`aiops_last_scan_timestamp_seconds`、`aiops_scan_ok`，主要 stage 從 0 開始輸出，否則 Prometheus 的 `increase()` 算不到第一次）。

`--demo` 的預期輸出：case2（OOMKilled）完成 patch → 驗證 → 學習並顯示 `RESOLVED`；case1、case3、case4 只輸出建議。

---

## 11. 部署

### 11.1 容器映像（`Dockerfile`）

- 基底：`python:3.12-slim`
- 安裝 `kubectl v1.31.0`（**固定為 linux/amd64**）
- 只複製 `aiops/` 與 `data/`；`ENTRYPOINT ["python", "-m", "aiops"]`

```powershell
docker build -t aiops-agent:local .
# k3s 用自己的 containerd，要把映像匯入（在 WSL 裡執行）
docker save aiops-agent:local | sudo k3s ctr images import -
```

### 11.2 叢集內部署（`deploy/`）

| 檔案 | 內容 |
| --- | --- |
| `agent-rbac.yaml` | `aiops-agent` ServiceAccount + Role（僅 `aiops-demo` namespace）：pods / pods/log / events 唯讀；deployments / replicasets 可 get/list/watch/patch/update |
| `agent-pvc.yaml` | 知識庫用的 `aiops-kb` PVC（local-path，100Mi） |
| `agent-rbac-travel.yaml` | 同樣的 Role 建在 `travel` namespace，讓代理也能監看、修復機票服務 |
| `agent-metrics.yaml` | `aiops-agent-metrics` Service + ServiceMonitor（Prometheus 抓 `:30080/metrics`） |
| `agent-deployment.yaml` | 單一副本代理（`Recreate` 策略，避免兩個 Pod 同時寫 KB），`imagePullPolicy: Never`（使用本機映像）；監看 `aiops-demo,travel`；網路搜尋 + LLM 自動修復開啟；KB 掛在 PVC `/kb`；**已啟用 LLM**，模型 `qwen2.5:7b`、confidence 門檻 0.6；image / env 白名單；`enableServiceLinks: false`（避免 `aiops-dashboard` Service 注入的 `AIOPS_DASHBOARD_PORT=tcp://…` 蓋掉設定）；驗證參數縮短（timeout 120s、stability 15s、poll 8s） |
| `ollama-endpoint.yaml` | 無 selector 的 Service + 手動 Endpoints，把叢集內的 `ollama:11434` 導向主機 IP `192.168.0.16`（WSL 上的 Ollama） |

```powershell
kubectl apply -f manifests/00-namespace.yaml
kubectl apply -f deploy/
```

---

## 12. 故障模擬案例（`manifests/`）

全部位於 `aiops-demo` namespace：

| 檔案 | 故障 | 造成方式 | 代理的處理 |
| --- | --- | --- | --- |
| `case1-crashloop.yaml` | CrashLoopBackOff | busybox 執行 `exit 1` | 規則 → 轉 KB/LLM，僅建議 |
| `case2-oomkilled.yaml` | OOMKilled | 64Mi 上限下配置 150MiB | **自動修復**：patch 為 512Mi → 驗證 → 學習（端到端展示） |
| `case3-imagepull.yaml` | ImagePullBackOff | `nginx:notfound` | **自動修復**（白名單 `nginx=nginx:1.27-alpine`）：換 image → 驗證 → 學習 |
| `case4-missing-env.yaml` | CrashLoopBackOff | 缺少 `DB_HOST` 環境變數 | **自動修復**（白名單 `DB_HOST=…`）：補 env → 驗證 → 學習 |
| `case5-pending.yaml` | Pending | 要求 256Gi 記憶體，沒有節點放得下 | 規則 → 建議降低 requests 或加節點 |
| `case6-readiness.yaml` | NotReady | readiness probe 打 `/healthz`，nginx 回 404 | **自動修復**：網路搜尋 + LLM → 改 probe 路徑（先打 Pod 驗證）→ 驗證 → 學習；之後直接用 KB |
| `case7-missing-configmap.yaml` | CreateContainerConfigError | env 參照不存在的 ConfigMap `app-config` | 規則 → 建議建立 ConfigMap 或修正參照 |

---

## 13. 測試（`tests/`）

```powershell
pip install pytest
pytest -q
```

| 檔案 | 涵蓋範圍 |
| --- | --- |
| `test_collector.py` | CrashLoop、lastState OOMKilled、ImagePull、CreateContainerConfigError、NotReady（含寬限期）偵測；健康 Pod 不產生 issue |
| `test_rules.py` | OOM 記憶體下限與倍增、無 Deployment 時為建議、ImagePull 白名單/非白名單/權限錯誤、MissingEnv 白名單/建議/讓給通用規則、ConfigRef、Pending、container 查找 |
| `test_pipeline.py` | KB patch 不跨 workload 套用、同 workload 可套用、精確規則不問 LLM、學習紀錄帶 target_name、LLM confidence 門檻、網路搜尋 → LLM 動作 → 套用、不安全/低信心動作只建議、驗證失敗 rollback 且不重試、pre-check 擋下 404 路徑、同 workload KB 優先、kubectl 逾時 |
| `test_actions.py` | 白名單動作的參數檢查（路徑、記憶體上限、只能調低 requests、未知動作）、MCP 回應解析、查詢去雜訊 |
| `test_kb.py` | 新增/搜尋往返、忽略未驗證紀錄、症狀不符不命中 |
| `test_util.py` | 記憶體單位解析與格式化、分詞、image repository 解析 |
| `test_dashboard.py` | workload 對應、事件時間軸、demo 掃描前後的儀表板狀態、HTTP 端點 |

目前結果：**93 passed**（代理 69 + `services/flight-watcher/tests` 24，從專案根目錄 `pytest -q` 一起跑）。

---

## 14. 觀察與待改進事項

閱讀程式碼時發現的問題，依影響程度排列：

1. **`--dry-run` 仍會執行驗證**：dry-run 時 `Remediator` 回傳 `success=True`，`Pipeline` 接著呼叫 `Validator`，對**未修改**的 Deployment 執行 `rollout status`。對仍在崩潰的 Pod，這會一直等到 `validation_timeout_seconds`（預設 300 秒）才判定失敗，使 `--once --dry-run` 可能卡住數分鐘。
2. ~~叢集內部署的 KB 不會持久化~~：已改用 PVC（見第 6 節）。
3. **OOMKilled 的 KB 紀錄目前不會被查詢**：`OOMKilledRule` 永遠回傳 `forward_to_kb=False` 的診斷，所以 OOM 案例不會走到 KB 搜尋，學到的 OOM 紀錄只寫不讀。
4. **KB 重複紀錄**：每次成功修復都會 append，沒有去重；目前已有兩筆幾乎相同的 case2 紀錄。（`--demo` 現在改寫到暫存檔，不再汙染 `data/`。）
5. **Pending / ContainerCreating 缺少上下文**（已加寬限期避免誤報，但仍沒有 events）：`_build_context` 只替 CrashLoopBackOff / Failed 收集 events，這些症狀送進 KB / LLM 時沒有 events 可用，而 events 正是判斷排程或掛載問題的關鍵。
6. **README 與實作不一致**：
   - README 說「stable for 5 min」，實際穩定期預設為 60 秒（300 秒是 rollout 逾時）。
   - 設定表缺少 8 個環境變數（見第 9 節），也沒有提到 `--demo`。
7. **環境綁定**：`Dockerfile` 固定下載 amd64 版 kubectl（ARM 主機無法使用）；`ollama-endpoint.yaml` 寫死主機 IP `192.168.0.16`。
8. **未使用的程式碼**：`LLMInput.container_spec` 目前沒有被呼叫。
9. **過期的快取**：`.pytest_cache/v/cache/lastfailed` 記錄了一個已不存在的測試 `test_oom_rule_builds_memory_patch`，可以刪除。

---

## 15. HomeLab 堆疊：監控 + 機票服務

從 Windows 開（Chrome / Edge 會自己把 `*.localhost` 解析到本機）：

| 網址 | 內容 |
| --- | --- |
| http://grafana.localhost:8000 | Grafana（帳號 admin，密碼：`kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}'` 再 base64 解碼） |
| http://flights.localhost:8000 | 機票追蹤網頁 |
| http://ntfy.localhost:8000/flights | ntfy 通知 + 聊天頻道 |
| http://localhost:30080 | 代理自己的即時儀表板 |

### 15.1 監控（`monitoring/`）

全部用 k3s 內建 helm-controller 的 `HelmChart` 物件安裝（不需要 helm CLI）：

| 檔案 | 內容 |
| --- | --- |
| `00-traefik-hostnetwork.yaml` | Traefik 改走 host network（WSL2 mirrored 只轉發真的在 listen 的 socket，NodePort / hostPort 從 Windows 連不到）；所有 Ingress 走 `:8000` |
| `10-kube-prometheus-stack.yaml` | Prometheus（保留 7 天、10Gi PVC）、Grafana（內建 Kubernetes 儀表板、Loki datasource）、node-exporter（關掉 root mount：WSL 的 `/` 不是 shared mount）、kube-state-metrics；k3s 沒有的 etcd/scheduler/controller-manager/proxy 監控關閉 |
| `20-loki.yaml` | Loki 3.7 Monolithic、filesystem 儲存在 local-path PVC（chart 內建 MinIO 將於 2026-10-31 移除）、保留 7 天、memcached 快取關閉 |
| `30-alloy.yaml` | Grafana Alloy（Promtail 已於 2026-03-02 EOL）：透過 API 收所有 Pod log + Kubernetes events 送進 Loki |
| `dashboards/` | `generate.py` 產生的儀表板 ConfigMap：**AIOps Agent**（未 Ready 的 Pod、自動修復/rollback 次數、KB 筆數、pipeline 事件、重啟排行、代理 log、警告事件）與 **Flight Deals**（各航線現價、價格走勢、航線表、檢查/指令次數、服務 log） |

### 15.2 機票追蹤（`services/flight-watcher/`）

- Google Flights 資料來自 `fli`（免 key），每 3 小時檢查一次每條航線；新增指令會立即查詢。
- 只在**新低價（比之前最低再低 3%）**或**跌破目標價**時通知，透過 ntfy 推播。
- 打字控制（網頁或 ntfy `flights` 頻道）：Ollama 解析自由文字，規則解析器當備援；規則抓到的日期/價格/天數優先（LLM 曾把「12月」解成到 2027-01-01）。
- `travel` namespace 也在 AIOps 代理監看範圍內。詳見 `services/flight-watcher/README.md`。

### 15.3 這個環境的注意事項

- 今晚 WSL 的 Ubuntu 曾多次自行重啟（k3s 跟著重啟）。`.wslconfig` 已設 `instanceIdleTimeout=-1`，這個設定要等 WSL 完整重啟（`wsl --shutdown`）後才會生效。
- Pod 用 Service 名稱連線時，記得 `enableServiceLinks: false`：Kubernetes 會注入 `<SERVICE>_PORT=tcp://…`，曾蓋掉 `AIOPS_DASHBOARD_PORT`，ntfy 也會把 `NTFY_*` 當設定讀。

---

## 16. Roadmap（摘自 README）

- **Phase 1（目前）**：Rule Engine + KB + 驗證 + 學習、網路搜尋輔助的白名單 LLM 修復（含 rollback）、Prometheus / Loki / Grafana 監控
- **後續**：
  - 向量知識庫（Qdrant / Chroma / Elasticsearch）
  - 拆分為 Multi-Agent 平台：Monitoring / Diagnosis / Repair / Validation / Knowledge agents
