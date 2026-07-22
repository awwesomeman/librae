# KDJ Oversold — 回溯驗證報告

| 項目 | 內容 |
|---|---|
| 決策資產 | BTCUSDT |
| 跨資產穩健性 | ETHUSDT（同參數不重調） |
| 基礎頻率 | H1 |
| 樣本切分 | IS-Train 2024-01-01~2024-12-31（8,984 bars）/ IS-Val ~2025-08-31（5,856 bars）/ OOS ~2026-07-01（7,297 bars） |
| 訊號 | `entry_signal` = KDJ(9,3) J 線 < `j_threshold`(20)，逐 bar 的 level-based 事件，非單次穿越 |
| 回測候選出場 | J 回升 > `exit_j_threshold`(80) 或持有滿 `max_hold_periods`(24 bars)，兩者先到者為準（`run.py` 本身沒有出場概念，只用於下方策略候選比較） |

未建立 `strategy.py`（因子驗證未過）。`run.py` 的 DB-signal-quality 監控不受本報告影響，繼續進行。

## 因子顯著性：`entry_signal` 事件 hit-rate（forward=24 bars）

`event_hit_rate`（二項檢定，H0: hit rate = 0.5，事件=J<20 的 bar）：

| 樣本 | n_events | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| IS-Train | 1696 | 0.5469 | 0.0001 | **0.0004（PASS）** |
| IS-Val | 1355 | 0.5193 | 0.1567 | 0.1567（fail） |
| OOS | 1672 | 0.4683 | 0.0099 | **0.0198（PASS）** |

Holm 校正（n=3）後：**IS-Train 顯著且方向正確**（oversold 後續上漲機率較高）；**IS-Val 不顯著**；**OOS 顯著但方向相反**（oversold 後續反而更常下跌）。三段樣本結論互相矛盾——沒有任何組合能支持一個穩定、方向一致的可用邊際。

## 策略候選比較 + 正式引擎回測（IS-Train+IS-Val，零成本）

`entry_signal` 本身就是唯一濾網（沒有另外的 gate 可拔），無濾網基準為 `always_enter`（KDJ 不參與進場決策，出場規則不變）：

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| kdj_signal | +63.97% | 1.052 | -25.16% | 527 |
| always_enter | +198.46% | 1.673 | -30.94% | 2297 |

樣本內 `always_enter` 明顯優於 `kdj_signal`——KDJ 濾網在 IS 上不只沒有加值，反而濾掉大半獲利機會。

## OOS 盲測

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| kdj_signal | -1.89% | 0.090 | -24.75% | 279 |
| always_enter | -46.99% | -1.620 | -53.72% | 1055 |

排序反過來：`kdj_signal` 大幅少虧，`always_enter` 重虧 47%。但這跟跨資產結果不一致（見下），更可能的解釋是 KDJ 把交易次數砍到 279（曝險大減）疊加這段窗口本身的反向顯著性（見上），不是挑對方向。

## MAE/MFE 分布（kdj_signal，IS-Train 進場事件）

314 個進場事件：median MAE = -1.09% / median MFE = 0.89% / P75 |MAE| = 2.27%。逆行幅度中位數比順行更深，方向上不利——跟一個有效訊號該有的樣貌相反。

## 跨資產穩健性（ETHUSDT，同參數，不重調）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| kdj_signal | -61.41% | -0.571 | -71.54% | 797 |
| always_enter | -41.62% | -0.041 | -75.86% | 3297 |

跟 OOS BTC 排序相反：ETH 上 `always_enter` 反而比 `kdj_signal` 少虧。BTC OOS 看到的「KDJ 讓虧損變小」換一個資產就反轉——資產特有的雜訊，不是可泛化的市場結構。

## 結論

**沒有 highlight。** `entry_signal`（KDJ(9,3) J<20）三段樣本 + 跨資產全部檢視後，沒有找到穩定、方向一致的可用邊際：

1. **因子顯著性方向不一致**：IS-Train 顯著且方向正確，IS-Val 不顯著，OOS 顯著但方向相反——正向邊際沒有延續到樣本外。
2. **樣本內 KDJ 濾網比無濾網基準差**：`always_enter` 的 Sharpe/淨報酬都明顯優於 `kdj_signal`——濾網在濾掉獲利,不是濾掉風險。
3. **OOS 與跨資產排序互相矛盾**：BTC OOS 上 KDJ 濾網「少虧」更像是曝險減少疊加反向顯著性，換到 ETH 排序就反轉。
4. **MAE 中位數(-1.09%) 幅度大於 MFE 中位數(0.89%)**，逆行普遍比順行更深，跟有效濾網該有的樣貌相反。
5. 以上皆為零成本數字，真實成本會讓已經不一致/多半虧損的數字更差。

**建議：不建議建立 `strategy.py`。** 若要繼續：換一組 KDJ 參數（更嚴格的 j_threshold、更長的 kdj_length）或換基礎頻率重新掃描，而不是在已測出方向不一致的參數上繼續調風控。

## 已知限制

- 只驗證了 BTC 決策 + ETH 穩健性。
- 未做持有期橫掃——24 bars 是候選策略拍板的出場地平線，這裡驗證的是這組參數下有沒有效，不是找更好的參數。
- `always_enter` 基準是「KDJ 完全不參與進場」的 ablation，不是嚴謹的 buy-and-hold，只用於回答「KDJ 這個濾網本身有沒有加值」。
- MAE/MFE 只算了 `kdj_signal` 在 IS-Train 的診斷，因子顯著性已不穩定，校準停損停利沒有意義。
