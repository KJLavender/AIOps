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
│   ├── dashboard.py           # 即時網頁儀表板（http.server，/ 與 /api/state）
│   ├── rules/
│   │   ├── base.py            # Rule 基底類別、RuleContext
│   │   ├── builtin.py         # 內建三條規則
│   │   └── engine.py          # RuleEngine：第一條符合的規則勝出
│   ├── knowledge_base/
│   │   ├── base.py            # KnowledgeBase 抽象介面
│   │   └── json_store.py      # JSONL 實作 + Jaccard 關鍵字評分
│   └── llm/
│       ├── base.py            # LLMAnalyzer 介面、LLMInput、NullAnalyzer
│       └── ollama.py          # Ollama /api/generate 實作
├── data/
│   └── knowledge_base.jsonl   # 知識庫資料（目前 3 筆 OOMKilled 紀錄）
├── manifests/                 # 故障模擬用的 K8s 資源（4 個案例）
├── deploy/                    # 代理本身部署到叢集的資源（RBAC、Deployment、Ollama 端點）
├── tests/                     # pytest 單元測試
├── Dockerfile                 # python:3.12-slim + kubectl v1.31.0
├── .dockerignore / .gitignore
├── requirements.txt           # 無執行期依賴；開發用 pytest
└── README.md                  # 原始英文說明
```

> 此工作區目前**不是 git repository**。

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
   │   KB.search ──命中──► Diagnosis(source=KB)
   │        │ 未命中
   │        ▼
   │   LLM.analyze（llm_enabled 且規則的 consult_llm=True 時）
   │        ──► Diagnosis(source=LLM, auto_fixable=False)；confidence 低於門檻則丟棄
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
   │
   ▼
LearningEngine.record (寫入 KB，verified=true)
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
| `NotReadyRule` | NotReady | 從 events 抓 readiness probe 失敗訊息，轉交 KB / LLM | ❌ 建議 |

**白名單原則**：代理從不「猜」正確的 image 或環境變數值。只有維運人員事先核准的值才會自動套用，其餘一律只給建議。

**OOM 記憶體計算**：`新上限 = max(目前上限 × memory_scale_factor, default_memory_limit)`

- 64Mi → 128Mi，但低於 512Mi 下限 → **512Mi**
- 512Mi → **1Gi**
- 沒有設定上限 → 512Mi
- Pod 沒有所屬 Deployment（裸 Pod）→ 只給建議

產生的 patch 會同時設定 `limits.memory` 與 `requests.memory`。

`ContainerCreating`、`Failed` 沒有對應規則，直接進入 KB / LLM 流程。

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
  - 呼叫 `POST {endpoint}/api/generate`，`format: "json"`，`stream: false`
  - Prompt 包含：症狀、訊息、events、logs、previous logs、describe、Deployment YAML（各截斷至 4000 字元）
  - 要求回傳 `{"root_cause", "fix", "confidence"}`；若 confidence > 1 會自動除以 100
  - confidence 低於 `llm_min_confidence`（預設 0.6）或沒有 fix 的回答**直接丟棄**，保留規則的診斷
  - 產生的 Diagnosis **一律 `auto_fixable=False`**
  - 呼叫失敗或無法解析 JSON 時回傳 None，不影響主流程

---

## 8. 修復與驗證

### 修復（`remediation.py`）

- 使用 `kubectl patch <kind> <name> -n <ns> --type strategic -p <json>`。
- `dry_run=True` 時只記錄「would patch」，不實際修改叢集。
- `patches.py` 提供三種 patch 產生器：`memory_limit_patch`（OOMKilledRule）、`image_patch`（ImagePullRule）、`env_patch`（MissingEnvRule）。

### 驗證（`validation.py`）

1. `kubectl rollout status deployment/<name> --timeout=<validation_timeout_seconds>s`
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

Pod 狀態是儀表板每次請求時直接向叢集查詢（快取 2 秒），所以 Agent 在等待驗證時畫面仍會即時更新。Agent 事件只存在記憶體，重啟後清空。

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
| `agent-deployment.yaml` | 單一副本代理（`Recreate` 策略，避免兩個 Pod 同時寫 KB），`imagePullPolicy: Never`（使用本機映像）；只監看 `aiops-demo`；KB 掛在 PVC `/kb`；**已啟用 LLM**，模型 `qwen2.5:7b`、confidence 門檻 0.6；image / env 白名單；`enableServiceLinks: false`（避免 `aiops-dashboard` Service 注入的 `AIOPS_DASHBOARD_PORT=tcp://…` 蓋掉設定）；驗證參數縮短（timeout 120s、stability 15s、poll 8s） |
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
| `case6-readiness.yaml` | NotReady | readiness probe 打 `/healthz`，nginx 回 404 | 規則 → 附 probe 失敗訊息，轉 KB/LLM |
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
| `test_pipeline.py` | KB patch 不跨 workload 套用、同 workload 可套用、精確規則不問 LLM、學習紀錄帶 target_name、LLM confidence 門檻 |
| `test_kb.py` | 新增/搜尋往返、忽略未驗證紀錄、症狀不符不命中 |
| `test_util.py` | 記憶體單位解析與格式化、分詞、image repository 解析 |
| `test_dashboard.py` | workload 對應、事件時間軸、demo 掃描前後的儀表板狀態、HTTP 端點 |

目前結果：**44 passed**。尚無 `validation`、`remediation` 的專門測試。

---

## 14. 觀察與待改進事項

閱讀程式碼時發現的問題，依影響程度排列：

1. **`--dry-run` 仍會執行驗證**：dry-run 時 `Remediator` 回傳 `success=True`，`Pipeline` 接著呼叫 `Validator`，對**未修改**的 Deployment 執行 `rollout status`。對仍在崩潰的 Pod，這會一直等到 `validation_timeout_seconds`（預設 300 秒）才判定失敗，使 `--once --dry-run` 可能卡住數分鐘。
2. ~~叢集內部署的 KB 不會持久化~~：已改用 PVC（見第 6 節）。
3. **OOMKilled 的 KB 紀錄目前不會被查詢**：`OOMKilledRule` 永遠回傳 `forward_to_kb=False` 的診斷，所以 OOM 案例不會走到 KB 搜尋，學到的 OOM 紀錄只寫不讀。
4. **KB 重複紀錄**：每次成功修復都會 append，沒有去重；目前已有兩筆幾乎相同的 case2 紀錄。（`--demo` 現在改寫到暫存檔，不再汙染 `data/`。）
5. **Pending / ContainerCreating 缺少上下文**：`_build_context` 只替 CrashLoopBackOff / Failed 收集 events，這些症狀送進 KB / LLM 時沒有 events 可用，而 events 正是判斷排程或掛載問題的關鍵。
6. **README 與實作不一致**：
   - README 說「stable for 5 min」，實際穩定期預設為 60 秒（300 秒是 rollout 逾時）。
   - 設定表缺少 8 個環境變數（見第 9 節），也沒有提到 `--demo`。
7. **環境綁定**：`Dockerfile` 固定下載 amd64 版 kubectl（ARM 主機無法使用）；`ollama-endpoint.yaml` 寫死主機 IP `192.168.0.16`。
8. **未使用的程式碼**：`LLMInput.container_spec` 目前沒有被呼叫。
9. **過期的快取**：`.pytest_cache/v/cache/lastfailed` 記錄了一個已不存在的測試 `test_oom_rule_builds_memory_patch`，可以刪除。

---

## 15. Roadmap（摘自 README）

- **Phase 1（目前）**：Rule Engine + KB + 驗證 + 學習
- **後續**：
  - 向量知識庫（Qdrant / Chroma / Elasticsearch）
  - 拆分為 Multi-Agent 平台：Monitoring / Diagnosis / Repair / Validation / Knowledge agents
