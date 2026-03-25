# AGENTS.md — quant-strategy-lab (Single Source of Truth)

本檔是本專案唯一 Agent 編排規範。

---

## 1) Agent 角色

| 角色 | 模型 | 職責 |
|------|------|------|
| **Backend** | `claude-opus-4-6` | 策略邏輯、回測引擎、指標模組、資料流、API |
| **Frontend** | `claude-sonnet-4-6` | Streamlit / Grafana 版面、圖表、顯示邏輯 |
| **Master** | `claude-opus-4-6` | 整合決策、交付驗收（預設兼任 QA） |
| **QA**（選用） | `claude-sonnet-4-6` | 獨立全域驗證（僅高風險里程碑啟用） |

**聯網查證**: 任何角色需要外部資訊時，使用 `gemini --search`。

**Fallback**:
- Opus → Sonnet 4.6 → Sonnet 4.5
- `gemini --search` 失敗 → `web_search` / `web_fetch`
- 啟用 fallback 時須在回報中註記原模型、fallback 模型、原因

---

## 2) Skills（強制）

- 預設安裝 `https://github.com/awwesomeman/python-skills#` 整包
- 至少確認 `git`, `python`, `quant` 可用
- 回報第一行必須標註 `Skills used: ...`
- Backend 預設：`python, quant`；Frontend 預設：`python`（涉及策略語意加 `quant`）

---

## 3) 回報格式（5 行，強制）

```
CHANGED: [檔案，最多 8 個，其餘 +N files]
CMDS:    [關鍵命令/測試]
TEST:    [pass x/y；失敗列前 3 個]
RISKS:   [風險/漏洞]
NEXT:    [建議下一步]
```

- 每行 ≤120 字
- 禁止貼長 log（改存檔案路徑）
- 禁止重述任務背景

---

## 4) Scope Isolation

- **Backend**: `nautilus_lab/nautilus_lab/strategies`, `backtest`, `monitoring`, `scripts`
- **Frontend**: `nautilus_lab/app`, `nautilus_lab/grafana`, `docs`（UI 相關）
- **Master/QA**: 全域可讀（僅里程碑觸發全域掃描）
- 跨域改動需 Master 授權

---

## 5) 測試門檻

- 每個交付：≥1 targeted test + ≥1 functional smoke
- 使用者回報 blocking issue → reopen → 修 → 重測 → 再回報

---

## 6) /simplify 觸發

- 多檔重構後
- 主要功能交付前
- 可讀性明顯下降
- 使用者明確要求
- Claude 實作的里程碑結束時預設執行一次

---

## 7) 里程碑 QA 觸發

- **M1**: 樣本資料完成，準備串回測
- **M2**: 指標計算完成，準備推 Streamlit/Grafana
- **M3**: 多資產擴展合併主分支前

---

## 8) 執行模式

- **預設單代理先行**（Master 判斷主面向）
- Master 兼任 QA
- 多代理時機：前後端 schema 同步改動 / 里程碑驗收 / 使用者要求並行

---

## 9) 變更規範

修改本檔 = 調整全專案 Agent 行為。commit message 加 `agents-policy`。
