# 文件導覽

根目錄的 [README](../README.md) 是專案入口；本目錄收錄任務導向指南與工程歷史紀錄。請優先閱讀描述現況的文件，只有在需要理解決策原因或實作歷程時，才往 decisions 與 plans 深入。

## 從這裡開始

| 需求 | 文件 |
|---|---|
| 安裝 Librae 或設定本機開發環境 | [Getting started](getting-started.md) |
| 執行一個完整策略 | [Examples](../examples/README.md) |
| 理解執行語意與系統設計 | [Architecture](../architecture.md) |
| 分析訊號的遠期表現 | [Signal outcome analysis](guides/signal-outcome-analysis.md) |
| 設定 DB、Grafana、broker 或部署環境 | [Optional infrastructure](guides/optional-infrastructure.md) |

## 文件類型

| 位置 | 用途 | 維護原則 |
|---|---|---|
| [`../architecture.md`](../architecture.md) | 系統現況與設計規範 | 行為或結構改變時，與程式碼同步更新 |
| `guides/` | 使用者與維運者的任務導向指南 | 確保指令可執行，深入細節改連結 reference |
| `decisions/` | 架構決策紀錄（ADR） | 保留當時觀點；以 supersede 取代重寫歷史 |
| `plans/` | 實作計畫與工作筆記 | 除非文件明確標示，否則狀態視為歷史資訊 |
| `research/` | 研究筆記與技術調查 | 清楚註明假設、資料範圍與結論 |
| `spikes/` | 限時實驗與框架評估 | 有長期影響的結論應整理到 decisions |
| `learnings/` | 錯誤與維運經驗 | 記錄現象、根因、修正與預防方式 |

## 資訊衝突時，以誰為準？

1. 測試與目前程式碼定義實際行為。
2. [`architecture.md`](../architecture.md) 說明預期的系統現況。
3. guides 說明如何在該現況下完成任務。
4. decisions 說明某個時間點為何做出選擇。
5. plans、research 與 spikes 是歷史輸入，不是 API 保證。

這個順序讓根 README 維持精簡，同時保留深入資訊的明確入口。
