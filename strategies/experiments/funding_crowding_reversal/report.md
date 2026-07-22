# Funding-Rate Crowding Reversal — 研究報告

| 項目 | 內容 |
|---|---|
| 決策資產 | BTCUSDT |
| 跨資產穩健性 | ETHUSDT（同參數不重調） |
| 基礎頻率 | H1 |
| 樣本切分 | IS-Train 2024-01-01~2024-12-31 / IS-Val ~2025-08-31 / OOS ~2026-07-01 |
| 測試因子 | `funding_rate_bps`、`funding_z_3d`、`funding_cum_3_bps`（資金費率擁擠代理）、`xasset_corr_24`、`xasset_relmom_24`（跨資產連動） |

未建立 `strategy.py`（因子驗證未過）。

## 外部數據因子篩選（IS-Train，directional_hit_rate，1H，1/4/12/24h 持有期）

五個因子在任一持有期，p-value 都落在 0.15~0.83 之間，Holm-Bonferroni 校正（n=20，FWER）後**沒有任何一格顯著**。

## 因子邊際穩定性（oos_decay，IS-Train 內部 70/30 切分）

| 因子 | 頻率 | 存活率 | 反號 | 狀態 |
|---|---|---|---|---|
| funding_rate_bps | 1h | 4.7569 | 否 | PASS |
| funding_z_3d | 12h | 8.0673 | 是 | VETOED |
| funding_cum_3_bps | 24h | 2.0884 | 否 | PASS |
| xasset_corr_24 | 12h | 2.4876 | 否 | PASS |
| xasset_relmom_24 | 1h | 0.6134 | 否 | PASS |

`funding_z_3d` 前後半段效應方向相反，被否決。其餘因子雖未反號，但前提（因子本身顯著）沒有成立，PASS 不構成加值證據。

## 基礎頻率橫掃（1H/4H/1D，僅 xasset_corr/xasset_relmom）

換 4H/1D 重新取資料橫掃，跨 base_tf×factor×horizon 一起做 Holm 校正（n=24）後，三個基礎頻率都沒有通過顯著性門檻——1H 上的不顯著不是頻率選錯。`funding` 系列因子的結算排程（每日 3 次固定時間）跟 OHLCV 基礎頻率無關，無法做這個橫掃。

## 策略候選比較（IS-Val，零成本）

| 候選 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| A：Funding Crowding Reversal（純逆勢） | -10.21% | -0.2321 | -19.98% | 584 |
| B：Funding Reversal + 跨資產動能確認 | -2.59% | -0.0062 | -12.97% | 320 |
| C：OHLCV-only 基準（無外部數據） | 3.03% | 0.2988 | -14.42% | 212 |

純 OHLCV 基準（C）直接贏過兩個外部數據候選；B 加了跨資產動能濾網後比 A 好一點，但仍遠不如 C。

## 盲測 OOS（候選 C，BTC，同參數）

| 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|
| 22.49% | 0.9279 | -17.64% | 266 |

## 跨資產穩健性（ETHUSDT，同參數，不重調）

| 樣本 | 淨報酬 | Sharpe | 最大回撤 | 交易次數 |
|---|---|---|---|---|
| IS-Val | 154.65% | 2.8673 | -22.95% | 240 |
| OOS | -16.86% | -0.2772 | -34.57% | 316 |

## 跨資產核心因子穩健性（`funding_z_3d`/`xasset_relmom_24`，24h forward）

| 資產 | 因子 | Hit Rate | p (raw) | p (Holm) |
|---|---|---|---|---|
| BTCUSDT | funding_z_3d | 0.5147 | 0.2268 | 0.9074 |
| BTCUSDT | xasset_relmom_24 | 0.5104 | 0.2735 | 0.9074 |
| ETHUSDT | funding_z_3d | 0.4708 | 0.9256 | 0.5949 |
| ETHUSDT | xasset_relmom_24 | 0.4786 | 0.9053 | 0.5949 |

BTC/ETH 上核心外部因子都不顯著——不是 BTC 特有雜訊掩蓋了 ETH 上才顯現的效應，兩邊都沒有。

## 結論

**不建議建立 `strategy.py`——資金費率擁擠 + 跨資產相對動能在本樣本窗口上沒有觀察到獨立於既有 OHLCV 因子之外的方向性邊際。**

1. 五個外部因子在 IS-Train 上全部不顯著（Holm 校正，n=20）。
2. `funding_z_3d` 額外被 oos_decay 否決（IS-Train 內部前後半段效應反號）。
3. 換 4H/1D 基礎頻率沒有拯救 xasset 因子；funding 系列結構上無法橫掃基礎頻率。
4. 純 OHLCV 基準在 IS-Val 上直接贏過兩個外部數據候選（Sharpe 0.30 vs -0.23/-0.01）。
5. 跨資產（BTC/ETH）上核心外部因子都不顯著。
6. 以上皆為零成本數字，不含手續費/滑價/資金費率持倉成本——真實成本只會讓已經偏弱的 A/B 數字更差。

外部因子能取得（`ccxt.binanceusdm` 公開端點）、能接進正式回測引擎，但目前沒有觀察到它加值——跟 `trendpullback`/`mtf_trend_rsi` 兩個純 OHLCV 家族的結論一致。

**下一步建議**：繼續在同一組資金費率定義上調參屬於過度擬合同一批數據，不建議；更值得做的是換一種籌碼定義（如現貨/合約成交量比、多空持倉比，Binance 也有免驗證公開端點，這次沒測）。

## 已知限制

- 只驗證了 BTC 決策 + ETH 穩健性。
- 候選 C（OHLCV-only 基準）的參數沿用合理預設值，未另外橫掃——它只是比較基準，不是本研究的驗證對象。
- 以上皆為零成本數字，不含手續費/滑價/資金費率持倉成本。
