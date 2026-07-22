# MTF Trend Slicing Regime — 回溯驗證報告

| 項目 | 內容 |
|---|---|
| 決策資產 | BTCUSDT |
| 跨資產穩健性 | ETHUSDT（同參數不重調） |
| 基礎頻率 | H1（gate=1D） |
| 樣本切分 | IS-Train 2024-01-01~2024-12-31 / IS-Val ~2025-08-31 / OOS ~2026-07-01 |
| 測試因子 | `mom_1D_10`（日線趨勢閘門）、`rsi_1H_14`（進出場觸發）、fng/dxy/vol regime 切片 |

未建立 `strategy.py`（因子驗證未過）。驗證的是實際部署因子（`mom_1D_10` 閘門 + `rsi_1H_14` 觸發），regime 切片只對 `rsi_1H_14` 做（跨 fng/dxy/vol 三軸）；未測 TXFR1（FNG/DXY 對非 crypto 資產一律回退中性預設值，測了也是白測）。

## 因子顯著性：mom_1D_10（forward=10 天）

| 樣本 | n | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| IS-Train | 386 | 0.5067 | 0.5764 | 1.0000 |
| IS-Val | 234 | 0.5333 | 0.4016 | 1.0000 |
| OOS | 295 | 0.5263 | 0.3660 | 1.0000 |

三個樣本都不顯著，跟丟硬幣沒有差異。

## 因子顯著性：rsi_1H_14 多頻率橫掃（IS-Train）

| Forward(h) | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|
| 1 | 0.4860 | 0.9985 | 0.0118 |
| 4 | 0.4827 | 0.9690 | 0.1239 |
| 12 | 0.4791 | 0.9050 | 0.1901 |
| 24 | 0.4467 | 0.9861 | 0.0832 |

1h 通過 Holm 校正（p_holm=0.0118），hit rate 0.4860 < 0.5，效應方向為反轉。

## rsi_1H_14 邊際穩定性（oos_decay，IS-Train 內部 70/30 切分，1h）

`survival_ratio=0.3159`、`sign_flipped=False`、**status=VETOED**——存活率遠低於 0.5 門檻，代表這個 1h 邊際在 IS-Train 內部自己都撐不住，很可能只是前 70% 那段期間的雜訊。

## Regime 切片檢定（IS-Train，rsi_1H_14 @ 1h）

依 Fear & Greed：

| Regime | Hit Rate | p-value |
|---|---|---|
| greed | 0.4853 | 0.9982 |
| fear | 0.4906 | 0.7578 |

依 DXY：

| Regime | Hit Rate | p-value |
|---|---|---|
| weak_dxy | 0.4862 | 0.9887 |
| strong_dxy | 0.4857 | 0.9745 |

依波動：

| Regime | Hit Rate | p-value |
|---|---|---|
| low_vol | 0.4852 | 0.9955 |
| high_vol | 0.4869 | 0.9574 |

三個 regime 軸上 hit rate 幾乎不隨切片變動（都落在 0.485~0.491），沒有任何一個 regime 下特別有效，跟 oos_decay 的 VETOED 一致。

## 部署訊號事件顯著性：with_filter vs no_filter（forward=24h）

| 樣本/版本/方向 | n_events | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| IS-Train/with_filter/long | 64 | 0.4375 | 0.3173 | 1.0000 |
| IS-Train/with_filter/short | 206 | 0.5245 | 0.4838 | 1.0000 |
| IS-Train/no_filter/long | 171 | 0.4503 | 0.1936 | 1.0000 |
| IS-Train/no_filter/short | 206 | 0.5245 | 0.4838 | 1.0000 |
| IS-Val/with_filter/long | 65 | 0.4462 | 0.3853 | 1.0000 |
| IS-Val/with_filter/short | 87 | 0.2644 | 0.0000 | **0.0001（PASS）** |
| IS-Val/no_filter/long | 92 | 0.4457 | 0.2971 | 1.0000 |
| IS-Val/no_filter/short | 87 | 0.2644 | 0.0000 | **0.0001（PASS）** |
| OOS/with_filter/long | 8 | 0.5000 | 1.0000 | 1.0000 |
| OOS/with_filter/short | 135 | 0.5630 | 0.1434 | 1.0000 |
| OOS/no_filter/long | 145 | 0.3724 | 0.0021 | **0.0212（PASS）** |
| OOS/no_filter/short | 135 | 0.5630 | 0.1434 | 1.0000 |

Holm 校正（n=12）後 3 格通過，**全部是方向相反的顯著**：空單「失敗」機率顯著高於 0.5（後續更常上漲）、多單訊號後續更常下跌——跟 `mtf_trend_rsi`/`trendpullback` 唯一通過校正的格子都是反方向的模式一致，不是找到可用邊際。

## 策略候選比較（IS-Val，零成本）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| with_filter | -30.98% | -0.5101 | -53.63% | 59 |
| no_filter | -23.74% | -0.2402 | -51.38% | 80 |

## 盲測 OOS

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| with_filter | 13.52% | 0.8371 | -17.02% | 26 |
| no_filter | -0.79% | 0.1131 | -23.88% | 41 |

**IS-Val 上加濾鏡的 Sharpe（-0.51）比不加濾鏡（-0.24）更差，OOS 上卻反過來（0.84 vs 0.11）**。兩個獨立窗口排序完全相反——regime 濾鏡加值與否不穩定，不能只憑其中一個窗口就宣稱濾鏡有效。

## MAE/MFE 分布（IS-Train，with_filter）

| n | median MAE | median MFE | P75(&#124;MAE&#124;) | P50(MFE) |
|---|---|---|---|---|
| 38 | -2.83% | 3.14% | 4.66% | 3.14% |

## 跨資產穩健性（ETHUSDT，同參數，不重調）

| 版本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| with_filter | -0.82% | 0.1779 | -48.52% | 95 |
| no_filter | -22.55% | -0.0217 | -54.29% | 121 |

ETH 上 with_filter 優於 no_filter，方向跟 BTC 的 OOS 一致，但跟 BTC 的 IS-Val 相反——進一步印證濾鏡加值與否不穩定，不足以推翻 BTC IS-Val 已經證偽的「濾鏡總是加值」假設。

## 結論

1. **核心因子都未通過驗證**：`mom_1D_10` 三個樣本 Holm 校正後全部不顯著；`rsi_1H_14` 唯一通過校正的 1h 格子被 oos_decay 否決（存活率僅 0.3159），regime 切片也看不出任何一個 regime 特別有效。
2. **regime 濾鏡加值與否本身不穩定**：IS-Val 上加濾鏡明顯更差，OOS 上卻反過來明顯更好，兩個獨立窗口排序相反。ETH 穩健性檢查跟 BTC-OOS 同方向，但不足以推翻 BTC-IS-Val 已證明的不一致。
3. **部署訊號事件顯著性沒有任何一格是正確方向的顯著**：通過 Holm 校正的 3 格全部方向相反——跟 `mtf_trend_rsi`/`trendpullback` 目前測到的每一格「通過校正」的顯著性都是反方向,這個 repo 三個已測家族的共同模式。
4. 以上皆為零成本數字，真實成本會讓已經不穩定/多數為負的數字更差。

**跟原始研究結論一致**（IS-Val、OOS 排序不一致，濾鏡加值與否不穩定，不可靠）——這次是在完全獨立的資料源、獨立的回測引擎、修正過的無前視偏誤閘門合併下重新驗證，兩套獨立實作互相印證了同一個現象：這個濾鏡設計的邊際價值在統計上就是不穩定的。

## 已知限制

- 只驗證了 BTC 決策 + ETH 穩健性，未測 TXFR1（FNG/DXY 對 TW 期貨恆為中性 fallback）。
- `mom_1D_10`（3 樣本）跟部署訊號事件顯著性（12 格）用了兩個獨立的 Holm 校正群組，若合併校正，結論方向不變（分開校正時已經是 1.0000 或方向相反）。
- regime 切片只做了 `rsi_1H_14`，未做 `mom_1D_10` × vol_regime——`mom_1D_10` 是連續值，`merge_htf_column` 會強制轉 bool，需要另一個保留連續值的 merge 寫法。
- MAE/MFE 只做分布描述，未疊加 SL/TP 重新回測——核心因子/濾鏡都未通過驗證，風控校準不是優先事項。
