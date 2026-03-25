# 2026-03 核心決策整理

## 1) 工具分工
- 定版回測績效展示：以 **Streamlit** 為主。
- 訊號監控與長期運行監控：以 **Grafana** 為主。
- 結論：不再把所有場景硬塞進 Grafana。

## 2) 績效解讀原則（事件型策略）
- 以「區間實績」為主、年化為輔。
- 必須揭露交易活躍度與曝險（避免和 Buy&Hold 失真比較）。

核心指標（九項）
1. Total Return
2. Max Drawdown
3. PF
4. Win Rate
5. Avg Trade Return
6. #Trades
7. Exposure Ratio
8. Account-level CAGR
9. Active-period CAGR

## 3) Streamlit 頁面收斂方向
- 主題：策略回測分析
- 章節：策略總覽 / 交易明細
- 模組：主圖、風險（MDD）、績效比較表、交易明細（PnL + Order Detail）
- 策略背景改為 JSON 管理（可擴展）

## 4) Schema 與命名治理
- 命名採 **snake_case** 一致化。
- 單位說明只放 Description：`(unit: xxx)`。
- 不在 key / value 寫死單位。
- Performance Analysis 與 Metric Guide 維持 single source of truth。

## 5) 協作與 agent 方針
- 偏好工具：Claude CLI / Gemini CLI（不走 ACP）。
- 角色分工：PM、後端、監控/治理 QA、量化研究。
- 開發期原則：優先一致性與完整性，避免 fallback 混用。
