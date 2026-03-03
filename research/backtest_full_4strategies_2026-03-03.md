# 四策略詳細版回測報告（Full Template）

生成時間：2026-03-03 UTC

## MultiFactorScore_v1.0-H1-L-MXFR1

### 1) 實驗設定
- 標的：MXFR1
- 資料源：Shioaji
- 樣本期間：Train 2024-01-01~2025-03-31 / Validation 2025-04-01~2025-06-30 / OOS 2025-07-01~2026-03-03
- 成本設定：

### 2) 核心績效（Train / Validation / OOS）
| 指標 | Train | Validation | OOS |
|---|---:|---:|---:|
| 交易次數 | 84 | 11 | 58 |
| 勝率 | 67.86% | 81.82% | 68.97% |
| 平均報酬率（每筆） | 0.11% | 0.23% | 0.14% |
| Profit Factor | 2.53 | 11.40 | 3.18 |
| 權益倍數（Equity Multiple） | 1.10 | 1.03 | 1.08 |
| 年化報酬 | 7.73% | 10.89% | 12.49% |
| 年化夏普 | 3.25 | 6.83 | 4.28 |
| 年化波動 | 2.30% | 1.52% | 2.76% |
| 最大回撤 | 1.09% | 0.25% | 0.63% |

### 3) 協議與參數
- protocol：strict: train-select, validation-check, oos-once
- chosen_params：`{'th': 70, 'bn': 3, 'en': 20, 'cost': 2.0}`

---

## TrendPullback_v1.0-H1-L-MXFR1

### 1) 實驗設定
- 標的：MXFR1
- 資料源：Shioaji
- 樣本期間：Train 2024-01-01~2025-03-31 / Validation 2025-04-01~2025-06-30 / OOS 2025-07-01~2026-03-03
- 成本設定：

### 2) 核心績效（Train / Validation / OOS）
| 指標 | Train | Validation | OOS |
|---|---:|---:|---:|
| 交易次數 | 64 | 15 | 48 |
| 勝率 | 71.88% | 66.67% | 87.50% |
| 平均報酬率（每筆） | 0.16% | 0.08% | 0.27% |
| Profit Factor | 3.36 | 1.67 | 12.61 |
| 權益倍數（Equity Multiple） | 1.11 | 1.01 | 1.14 |
| 年化報酬 | 8.61% | 4.86% | 21.59% |
| 年化夏普 | 3.57 | 1.64 | 8.87 |
| 年化波動 | 2.33% | 2.92% | 2.21% |
| 最大回撤 | 1.29% | 1.01% | 0.28% |

### 3) 協議與參數
- protocol：strict: train-select, validation-check, oos-once
- chosen_params：`{'pull': 0.35, 'bn': 3, 'en': 20, 'tstop': 4, 'cost': 2.0}`

---

## MultiFactorScore_v1.0-H1-L-BTCF

### 1) 實驗設定
- 標的：BTCUSDT
- 資料源：Binance USDS-M Futures
- 樣本期間：Train 2025-01-01~2025-07-31 / Validation 2025-08-01~2025-09-30 / OOS 2025-10-01~2026-03-03
- 成本設定：

### 2) 核心績效（Train / Validation / OOS）
| 指標 | Train | Validation | OOS |
|---|---:|---:|---:|
| 交易次數 | 71 | 13 | 26 |
| 勝率 | 71.83% | 76.92% | 69.23% |
| 平均報酬率（每筆） | 0.16% | 0.17% | 0.19% |
| Profit Factor | 2.03 | 3.12 | 2.44 |
| 權益倍數（Equity Multiple） | 1.12 | 1.02 | 1.05 |
| 年化報酬 | 21.23% | 14.06% | 12.09% |
| 年化夏普 | 3.18 | 4.52 | 3.15 |
| 年化波動 | 6.11% | 2.92% | 3.64% |
| 最大回撤 | 2.60% | 0.61% | 0.94% |

### 3) 協議與參數
- protocol：strict: train-select, validation-check, oos-once
- chosen_params：`{'th': 75, 'cost': 8, 'bn': 3, 'en': 15}`

---

## TrendPullback_v1.0-H1-L-BTCF

### 1) 實驗設定
- 標的：BTCUSDT
- 資料源：Binance USDS-M Futures
- 樣本期間：Train 2025-01-01~2025-08-31 / Validation 2025-09-01~2025-10-31 / OOS 2025-11-01~2026-03-03
- 成本設定：

### 2) 核心績效（Train / Validation / OOS）
| 指標 | Train | Validation | OOS |
|---|---:|---:|---:|
| 交易次數 | 102 | 18 | 15 |
| 勝率 | 77.45% | 77.78% | 86.67% |
| 平均報酬率（每筆） | 0.24% | 0.23% | 0.36% |
| Profit Factor | 3.29 | 6.02 | 9.27 |
| 權益倍數（Equity Multiple） | 1.28 | 1.04 | 1.06 |
| 年化報酬 | 45.23% | 28.21% | 17.39% |
| 年化夏普 | 5.84 | 7.84 | 5.12 |
| 年化波動 | 6.43% | 3.18% | 3.14% |
| 最大回撤 | 2.19% | 0.36% | 0.56% |

### 3) 協議與參數
- protocol：strict: train-select, validation-check, oos-once
- chosen_params：`{'pull': 0.35, 'bn': 3, 'en': 10, 'tstop': 4, 'cost': 8}`

---
