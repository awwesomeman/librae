# AGENTS.md — quant-strategy-lab (Single Source of Truth)

本檔是本專案唯一 Agent 編排規範（single truth）。
所有任務分工、CLI 路由、回報格式與驗收門檻，以本檔為準。

---

## 0) 目標與原則

- 目標：在高效率下維持可驗證交付（correctness > fancy output）。
- 原則：
  1. 小上下文、強隔離（避免 Agent 互相污染）
  2. 先功能可跑，再做結構優化
  3. 無測試與 smoke，不宣稱完成
  4. 回報必須短格式（5 行）

---

## 1) Agent 角色定義

## 1.1 Backend Agent（GPT-5.3 orchestrator + Claude CLI）

**定位**
- GPT-5.3 負責量化邏輯拆解與任務編排
- 代碼實作預設轉交 Claude CLI（優先 Opus 4.6）

**工作範圍**
- 策略邏輯、回測引擎、指標模組、資料流、API schema

**硬性限制**
- 預設僅讀寫 backend 範圍檔案（見第 4 節 Scope Isolation）
- 禁止主動讀 UI 代碼（除非 Master 明確授權）

---

## 1.2 Frontend Agent（GPT-5.3 orchestrator + Gemini CLI）

**定位**
- GPT-5.3 規劃 UI 結構
- 大規模 UI 產碼、長文本轉換優先交 Gemini CLI

**工作範圍**
- Streamlit / Grafana 版面、圖表配置、顯示邏輯

**硬性限制**
- 僅依賴 backend 提供的 schema / contract（禁止自行改 backend 欄位語意）

---

## 1.3 Master / QA（GPT-5.3，必要時調 Gemini CLI）

**定位**
- Master 做最終決策與交付整合
- QA 預設由 Master 擔任；只有在高風險里程碑才啟用獨立 QA 子代理

**工作範圍**
- 全域一致性檢查、整合測試、回歸驗收、風險判讀

**Gemini CLI 使用時機（QA）**
- 需要全域掃描大量檔案、跨目錄一致性檢查時

---

## 2) CLI 路由規則（Tool Redirection）

系統規則：
- 複雜重構 / 算法修正 / 回測引擎修改 → **優先 Claude CLI**
- 大規模 UI 生成 / 長文本頁面 / 全域檔案掃描 → **優先 Gemini CLI**
- 任何子代理輸出一律轉為 5 行摘要回報（見第 3 節）

### 2.1 模型版本 Pin（強制）

- Backend Agent（Claude CLI）：預設固定 `claude-opus-4-6`
- Frontend / 全域掃描（Gemini CLI）：預設使用 `gemini-3-auto`（由 CLI 自動選擇 Pro/Flash）
- Master（OpenClaw 主會話）：依當前 OpenClaw session model 執行，不額外切換

### 2.2 Fallback 規則（強制）

僅在以下情況可啟用 fallback：
1. 指定模型暫時不可用
2. CLI 回傳配額/連線錯誤且重試失敗

Fallback 順序：
- Claude CLI：`claude-opus-4-6` → `claude-sonnet-4-5`
- Gemini CLI：`gemini-3-auto` → `gemini-2.5-flash`

啟用 fallback 時，回報格式需在 `CMDS` 或 `RISKS` 明確註記：
- 原模型
- fallback 模型
- 觸發原因

---

## 3) 子代理統一回報格式（強制）

每次回報必須只有以下 5 行語意：

1. `CHANGED:` 修改檔案清單
2. `CMDS:` 執行的關鍵命令/測試
3. `TEST:` 測試結果（Pass/Total）
4. `RISKS:` 邏輯漏洞、效能隱憂、資料風險
5. `NEXT:` 建議 Master 的下一步

**Token 控制格式（強制）**
- 每行盡量精簡（建議 ≤120 字）。
- `CHANGED` 最多列 8 個檔案，其餘以 `+N files` 表示。
- `TEST` 僅回傳摘要（如 `pass 18/18`）；失敗時最多列前 3 個 failed test 名稱。
- 禁止貼長篇測試 log；完整輸出改存檔案路徑供 Master 需要時查閱。
- 禁止重述整段任務背景，只回報執行結果與決策資訊。

禁止長篇 narrative，除非 Master 明確要求。

---

## 4) Scope Isolation（上下文隔離）

預設範圍：
- **Backend Agent**：
  - `nautilus_lab/nautilus_lab/strategies`
  - `nautilus_lab/nautilus_lab/backtest`
  - `nautilus_lab/nautilus_lab/monitoring`
  - `nautilus_lab/scripts`（限後端腳本）
- **Frontend Agent**：
  - `nautilus_lab/app`
  - `nautilus_lab/grafana`
  - `docs`（僅 UI/儀表板說明）
- **Master/QA**：全域可讀，但僅在里程碑觸發時做全域掃描

任何跨域改動需 Master 明確授權。

---

## 5) 測試與交付門檻（最小標準）

每個交付至少滿足：
1. **1 個 targeted test**（單元或整合）
2. **1 個 functional smoke run**（腳本/服務可啟動）

交付前（Master Gate）：
- 主要路徑測試全綠
- 指令可重現
- 風險已明示

若使用者回報 blocking issue：
- 一律 reopen 任務
- 修復後重新測試再回報

---

## 6) /simplify 觸發策略（非每步強制）

在下列時機觸發：
1. 多檔重構後
2. 主要功能完成準備交付前
3. 程式可讀性下降或重複碼明顯時
4. 使用者明確要求再優化

避免每微步驟都跑，兼顧 token 與時間效率。

---

## 7) 里程碑觸發 QA（Milestone Triggers）

Master 僅在下列節點強制全域 QA：
- **M1**：樣本資料生成完成，準備串回測引擎
- **M2**：指標計算完成，準備推送 Streamlit/Grafana
- **M3**：多資產擴展（如配對交易）合併主分支前

---

## 8) 預設執行模式（成本可控）

- **預設單代理先行**：先由 Backend 或 Frontend 其中一個代理執行（由 Master 判斷主任務面向）。
- Master 直接承擔 QA（不預設開 QA 子代理）。
- 只有在下列情況才升級為多代理：
  1. 涉及前後端 schema/contract 同步改動
  2. 進入 M1/M2/M3 里程碑驗收
  3. 使用者明確要求並行加速
- 高風險里程碑可再開第 3 個獨立 QA 子代理。
- 目標：降低溝通成本與 token 消耗，同時保留驗收品質。

---

## 9) OpenClaw 指令樣板（示意）

- Backend：
  - 「請調用 claude-cli 實作 Lumibot 回測邏輯；完成後以 5 行回報」
- Frontend：
  - 「請調用 gemini-cli 依 backtest schema 產生 Streamlit 儀表板；完成後 5 行回報」
- Master/QA：
  - 「請調用 gemini-cli 全域掃描，檢查前後端 schema 一致性；只回報測試結果與風險」

---

## 10) 變更規範

修改本檔即代表調整全專案 Agent 操作行為。
若規則更新，請在 commit message 註明 `agents-policy`。
