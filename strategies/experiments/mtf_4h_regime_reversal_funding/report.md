# MTF 4H Regime-Switching Reversal + Funding — 回溯驗證報告

| 項目 | 內容 |
|---|---|
| 決策資產 | BTCUSDT |
| 跨資產穩健性 | ETHUSDT（同參數不重調） |
| 基礎頻率 | 4H（daily regime gate，`mom_1D_10`） |
| 樣本切分 | IS-Train 2024-01-01~2024-12-31 / IS-Val ~2025-08-31 / OOS ~2026-07-01 |
| 持有期 | range-mode 12 根 4H bar（48h）/ trend-mode 16 根 4H bar（64h），部署參數，未橫掃 |

未建立 `strategy.py`（因子驗證未過）。

**原始報告的 headline 因子 `vwap_dist_12` 從未被實際計算過**——只存在於敘述性表格（宣稱 p_2s=0.0796、hit rate 0.6818、regime-switching 複合策略 IS-Val Sharpe 4.9682），原始可執行腳本完全沒有這個因子的計算程式碼。本報告第一次真的把它實作並跑出來（見下）。

## 因子顯著性：三個連續因子

### `mom_1D_10`（D1，forward=10 天）

| 樣本 | n | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| IS-Train | 390 | 0.5132 | 0.5050 | 1.0000 |
| IS-Val | 234 | 0.5333 | 0.4016 | 1.0000 |
| OOS | 295 | 0.5263 | 0.3660 | 1.0000 |

全部不顯著。

### `vwap_dist_12`（D1，forward=12 天）——原始報告的 headline 宣稱

| 樣本 | n | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| IS-Train | 389 | 0.5921 | 0.1269 | 0.3807 |
| IS-Val | 233 | 0.4318 | 0.8384 | 1.0000 |
| OOS | 294 | 0.4561 | 0.7926 | 1.0000 |

全部不顯著，IS-Val/OOS 甚至翻到 0.5 以下。**原始報告宣稱的「PASS」（p_2s=0.0796, hit rate 0.6818）完全沒有被獨立重現**——用本 repo 真實資料，這個因子測不出顯著性。

### `funding_z_3d`（4H，forward=16 bars）

| 樣本 | n | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| IS-Train | 2396 | 0.4701 | 0.8713 | 1.0000 |
| IS-Val | 1464 | 0.5096 | 0.4259 | 1.0000 |
| OOS | 1825 | 0.5028 | 0.4435 | 1.0000 |

全部不顯著。

## 部署邏輯的 regime-switching 進場訊號（事件 hit rate，依實際觸發時的 regime 拆分）

| 樣本/regime/方向 | n_events | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| IS-Train/range/long | 7 | — | — | (n<8, skipped) |
| IS-Train/range/short | 9 | 0.4444 | 1.0000 | 1.0000 |
| IS-Train/trend/long | 76 | 0.6579 | 0.0059 | 0.0591 |
| IS-Train/trend/short | 25 | 0.5000 | 1.0000 | 1.0000 |
| IS-Val/range/long | 14 | 0.7857 | 0.0574 | 0.5164 |
| IS-Val/range/short | 5 | — | — | (n<8, skipped) |
| IS-Val/trend/long | 19 | 0.6842 | 0.1671 | 1.0000 |
| IS-Val/trend/short | 13 | 0.6667 | 0.3877 | 1.0000 |
| OOS/range/long | 12 | 0.5000 | 1.0000 | 1.0000 |
| OOS/range/short | 8 | 0.6250 | 0.7266 | 1.0000 |
| OOS/trend/long | 26 | 0.5000 | 1.0000 | 1.0000 |
| OOS/trend/short | 16 | 0.5000 | 1.0000 | 1.0000 |

Holm 校正（n=10）後沒有任何一格通過。最接近的 `IS-Train/trend/long`（p_holm=0.0591）在 IS-Val/OOS 沒有重現同等強度（IS-Val hit rate 0.6842 但 n=19；OOS 直接掉到 0.5000）。

## 策略候選比較：switching 是否優於它自己的兩個組成子策略

用 `force_regime` 消融拆成三個候選：`switching`（部署邏輯）、`range_only`（永遠用 range-mode）、`trend_only`（永遠用 trend-mode）。零成本數字。

IS-Train+IS-Val：

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| switching | -4.07% | -0.049 | -20.79% | 101 |
| range_only | -29.23% | -0.746 | -43.99% | 53 |
| trend_only | -2.46% | -0.013 | -30.34% | 111 |

OOS 盲測：

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| switching | +3.99% | 0.414 | -10.30% | 45 |
| range_only | -10.62% | -0.627 | -19.28% | 26 |
| trend_only | +7.67% | 0.708 | -8.66% | 52 |

**直接反駁原始報告「regime-switching 複合策略最佳」的宣稱**：`trend_only`（純資金費率順勢，完全不切換）的 Sharpe/淨報酬在 IS-Train+Val 跟 OOS 都優於 `switching`。動態切換混入較弱的 range-mode 訊號，反而拖累整體表現。原始報告宣稱切換後 Sharpe 達 4.9682、最大回撤鎖在 -1.64%，跟本報告用真實引擎跑出的數字（switching OOS Sharpe 僅 0.414，最大回撤 -10.30%）差了一個量級。

## MAE/MFE 分布（switching 複合，IS-Train 進場事件）

70 個進場事件：median MAE = -1.40% / median MFE = +1.32% / P75 |MAE| = 2.69%。因子本身不顯著，僅記錄分布，不套用 SL/TP。

## 跨資產穩健性（ETHUSDT，同參數，不重調）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| switching | -17.74% | -0.236 | -41.93% | 172 |
| range_only | -40.76% | -0.400 | -61.47% | 105 |
| trend_only | -20.62% | -0.374 | -30.66% | 162 |

排序反過來——ETH 上 `switching` 最好，`range_only` 依然最差。跟 BTC OOS 排序不一致，三個資產/樣本沒有一個版本能一致勝出（ETH 上三個版本全部虧損，switching 只是虧得比較少）。

## 結論

1. 三個連續因子跟部署的多空進場訊號，Holm 校正後沒有任何一格顯著——最接近的一格（p_holm=0.0591）也沒有跨過門檻，且在 IS-Val/OOS 沒有重現。
2. **原始報告的 headline 因子 `vwap_dist_12` 從未被實際計算過**（只存在於敘述性表格）——本報告第一次真的跑出來，結果三個樣本都不顯著。
3. **原始報告宣稱的「regime-switching 複合策略最佳」沒有被獨立驗證支持**：消融實驗顯示 `switching` 在 BTC 上輸給更簡單的 `trend_only`；換到 ETH 又反過來——三個候選在三個樣本/資產組合裡排序不一致，沒有一個版本能穩定勝出。
4. 以上皆為零成本數字，真實成本會讓本來就搖擺不定、多數為負的數字更差。

**建議：不建立 `strategy.py`。** 若要繼續，應先確認 `trend_only`（純資金費率順勢）這個更簡單的子策略本身是否有獨立、可跨資產泛化的邊際（目前看起來也沒有——ETH 上一樣是負報酬），而不是在測不出顯著性的切換機制上繼續調參。

## 已知限制

- 只驗證了 BTC 決策 + ETH 穩健性。
- 未做持有期橫掃——12/16 bar 是部署參數，這裡驗證的是「這個參數下有沒有效」，不是找更好的參數。
- `funding_z_3d` 用共用模組現成的滾動 9 期（約 3 天）z-score 定義，跟原始腳本手刻的「4H bar 上滾動 6 期（24h）」視窗不完全相同，量級可比但非逐數字重現。
- 部署訊號顯著性檢定裡，`n_events<8` 的兩格被跳過——range-mode 進場事件數本身偏少，是這個訊號的固有限制。
- MAE/MFE 只做了 switching 複合在 IS-Train 的診斷，未疊加 SL/TP 回測驗證。
