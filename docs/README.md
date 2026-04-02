# docs/ 資料夾分類準則

## decisions/

記錄當下的技術決策與思考過程。每份文件捕捉「在那個時間點，我們為什麼做這個選擇」，包含背景分析、方案比較、與最終決定。

- 以日期命名（`YYYY-MM-DD-主題.md`）
- 決策一旦寫下不回溯修改原文，保留當時的判斷脈絡
- 若實作過程與原始規劃有差異，在文件末尾新增 `## 實作差異` section 記錄偏離原因與實際做法

**文件開頭必填欄位：**

```
> 狀態：proposed | accepted | implemented | superseded
> 範圍：影響的模組或系統（e.g. schema, engine, grafana）
> 前置決策：相關的 decision 連結（如有）
> 動機：一句話說明為什麼需要這個決策
```

狀態定義：

| 狀態 | 說明 |
|------|------|
| `proposed` | 提案中，尚未確認 |
| `accepted` | 已確認方向，尚未實作 |
| `implemented` | 已實作完成 |
| `superseded` | 被後續決策取代，標注取代者 |

## plans/

整合一或多份 decisions 的內容，經過優化後產出的實際執行規劃。Plans 是可操作的、有步驟的實作藍圖。

- 聚焦於「怎麼做」而非「為什麼」
- 會引用相關 decisions 作為依據
- 隨實作進展持續更新，每個 Step 標注 ✅ 或待驗收

**文件開頭必填欄位：**

```
> 狀態：planning | in-progress | completed | abandoned
> 範圍：影響的模組或系統
> 建立日期：YYYY-MM-DD
> 最後更新：YYYY-MM-DD
> 依據：相關的 decision 連結
```

## guides/

操作指南與部署文件。記錄可重複執行的步驟，供團隊成員或未來的自己參考。

## learnings/

開發過程中遭遇的問題與解法。記錄錯誤訊息、根因分析、與修復方式，避免重複踩坑。
