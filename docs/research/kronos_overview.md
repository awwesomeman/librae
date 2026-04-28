# Kronos: Financial Market Foundation Model 研究筆記

> **Paper**: "Kronos: A Foundation Model for the Language of Financial Markets" (arXiv:2508.02739, AAAI 2026)
> **Repo**: https://github.com/shiyu-coder/Kronos
> **Models**: HuggingFace `NeoQuasar` organization
> **License**: MIT

---

## 1. 模型架構

Kronos 是**第一個專為金融 K 線序列設計的開源基礎模型**，預訓練於全球 45+ 交易所的數據。不同於通用時序模型（TimesFM, Chronos, Moirai）將金融數據降維為單變量收盤價序列，Kronos 將 OHLCV 視為一等公民。

### Stage 1: Hierarchical Tokenizer (`KronosTokenizer`)

將連續、多維的 OHLCV 數據量化為離散 Token，使用 **Binary Spherical Quantization (BSQ)** 產生階層式雙層 Token：

- **s1 token（粗粒度）**: 10 bits，詞彙量 1024 — 捕捉大趨勢
- **s2 token（細粒度）**: 10 bits，詞彙量 1024 — 捕捉微觀波動

Tokenizer 架構為 encoder-decoder Transformer：
1. 輸入 OHLCV → 線性投影至 `d_model` 維度
2. Encoder Transformer blocks 處理序列
3. 線性層投影至 `codebook_dim`（s1_bits + s2_bits = 20）
4. BSQuantizer 以 sign function + straight-through gradient 量化
5. Decoder 從量化表示重建，s1-only 和 full (s1+s2) 各有獨立 decoder
6. 訓練損失 = reconstruction MSE + BSQ entropy penalty + commit loss

### Stage 2: Autoregressive Predictor (`Kronos`)

Decoder-only Transformer，在離散 Token 上訓練，學習預測下一個 Token（即下一個時間點的 K 線形態）：

- **RoPE** (Rotary Position Embeddings) 作為位置編碼
- **HierarchicalEmbedding**: 分別 embed s1/s2 token，concat 後經 fusion layer 投影回原維度
- **TemporalEmbedding**: 加入時間特徵（minute, hour, weekday, day, month），支援固定正弦或可學習
- **DualHead 預測頭**: 兩階段解碼 — 先預測 s1 logits，再透過 `DependencyAwareLayer`（cross-attention）條件預測 s2 logits
- **RMSNorm**（非 LayerNorm）、SiLU-gated FFN（類 LLaMA 風格）

### Model Zoo

| Model | Tokenizer | Context Length | Parameters | Available |
|---|---|---|---|---|
| Kronos-mini | Kronos-Tokenizer-2k | 2048 | 4.1M | Yes |
| Kronos-small | Kronos-Tokenizer-base | 512 | 24.7M | Yes |
| Kronos-base | Kronos-Tokenizer-base | 512 | 102.3M | Yes |
| Kronos-large | Kronos-Tokenizer-base | 512 | 499.2M | Closed |

---

## 2. 數據格式與 API

### 輸入要求

- **必要欄位**: `open`, `high`, `low`, `close`
- **可選欄位**: `volume`, `amount`（缺失時填零）
- **時間戳**: 可被 `pd.to_datetime` 解析，用於 temporal embedding（分鐘到月度解析度）
- **正規化**: `KronosPredictor` 自動處理 instance-level z-score normalization（clip 至 [-5, 5]），預測結果自動逆正規化

```csv
timestamps,open,high,low,close,volume,amount
2024-06-18 11:15:00,11.27,11.28,11.26,11.27,379.0,427161.0
```

### 核心 API — `KronosPredictor`

```python
from model import Kronos, KronosTokenizer, KronosPredictor

tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
predictor = KronosPredictor(model, tokenizer, device="cuda:0", max_context=512)

pred_df = predictor.predict(
    df=x_df,               # DataFrame with OHLC[VA] columns
    x_timestamp=x_ts,      # pd.Series of historical timestamps
    y_timestamp=y_ts,       # pd.Series of future timestamps to predict
    pred_len=120,           # number of bars to predict
    T=1.0,                  # sampling temperature
    top_p=0.9,              # nucleus sampling threshold
    sample_count=5,         # number of paths to average (ensemble)
    verbose=True
)
# Returns: DataFrame with columns [open, high, low, close, volume, amount]
```

### 關鍵參數

| 參數 | 說明 |
|---|---|
| `max_context` | 最大序列長度（small/base: 512, mini: 2048）|
| `T` | 取樣溫度 — 越高越多樣，越低越確定性 |
| `top_p` | Nucleus sampling 閾值，建議 0.9 |
| `sample_count` | 獨立預測路徑數量，取平均做 ensemble |
| `clip` | z-score clipping 閾值，預設 5 |

### Batch Prediction

`predictor.predict_batch()` — 在 GPU 上平行處理多條序列，所有序列需共享相同 lookback 和 pred_len。適合多標的同時預測。

---

## 3. 單一資產交易應用流程

### Pipeline

1. **數據特徵工程**: 確保資產的數據頻率（Daily, 1H, 15M）與模型訓練頻率一致
2. **預測生成**: 模型輸入過去 $N$ 個 K 線（look-back window），輸出未來 $M$ 個 K 線的預測分佈
3. **訊號提取**:
   - **點預測**: 預測下一根 K 線的漲跌幅
   - **分佈預測**: 利用多路徑 sampling 計算預期回報與風險指標

### 基礎策略邏輯

```
每日收盤後：
1. 將過去 60 天的 OHLCV 餵入 Kronos
2. 預測未來 T+1 天的收益率 r_hat
3. 若 r_hat > θ → 做多；若 r_hat < -θ → 做空或平倉
4. 利用多路徑 sampling 計算預測置信度 → 路徑高度分歧時減少部位
```

### 訊號提取方式（from backtest code）

| Signal | 計算方式 |
|---|---|
| `last` | predicted_close[-1] - current_close |
| `mean` | mean(predicted_closes) - current_close |
| `max` | max(predicted_closes) - current_close |
| `min` | min(predicted_closes) - current_close |

---

## 4. Fine-tuning

雖然 Kronos 具備 zero-shot 預測能力，但特定資產上的微調通常能顯著提升 RankIC 或降低 MAE。

### Pipeline A: Qlib-based（A 股市場）

位於 `finetune/`，使用 Microsoft Qlib 管理數據：

```bash
# 1. 設定 finetune/config.py（paths, instrument, time ranges, hyperparams）
# 2. 預處理
python finetune/qlib_data_preprocess.py
# 3. 微調 tokenizer
torchrun --nproc_per_node=N finetune/train_tokenizer.py
# 4. 微調 predictor
torchrun --nproc_per_node=N finetune/train_predictor.py
# 5. 回測
python finetune/qlib_test.py --device cuda:0
```

### Pipeline B: CSV-based（任意數據）

位於 `finetune_csv/`，使用 YAML config：

```bash
python train_sequential.py --config configs/config_ali09988_candle-5min.yaml
```

支援 sequential training（先 tokenizer 後 predictor）、DDP 多 GPU、skip flags。

### 微調建議

- **學習率**: 極小值（1e-5 或 5e-6），搭配 Weight Decay，防止 Catastrophic Forgetting
- **Full Fine-tuning**: 適合數據量充足的情況
- **Adapter/LoRA**: 資源有限時僅微調部分層
- **Domain Adaptation**: 預訓練以美股為主時，需在目標市場（如加密貨幣）數據上微調
- **Rolling Fine-tune**: 市場環境劇變（Regime Switch）後，使用最近一年數據滾動微調
- **訓練細節**: AdamW + OneCycleLR，gradient clipping（tokenizer: 2.0, predictor: 3.0）

---

## 5. 回測

### Qlib 回測（內建）

使用 Qlib 的 `TopkDropoutStrategy` — 按預測收益排名選 top-K 股票：
- 交易成本：0.1% open + 0.15% close commission，5 元最低，9.5% 漲跌幅限制
- 報告超額收益（含/不含成本），生成累積收益曲線 vs benchmark（CSI300/CSI800/CSI1000）

> **注意**: 這是簡化的 demo 回測。生產級策略需要投組最適化、風險因子中性化、滑點/市場衝擊建模。

### Web UI

位於 `webui/`，Flask-based（port 7070）：
- 互動式 K 線圖（Plotly）
- 模型選擇（mini/small/base）
- 檔案上傳 + 參數設定預測
- 預測 vs 實際對比
- 結果存為 JSON

---

## 6. 與 quant-strategy-lab 的整合思路

### 作為 Alpha Signal Generator

Kronos 可作為 librae 策略框架的**訊號源**：

| 整合模式 | 說明 |
|---|---|
| **Signal Generator** | predicted_close_change 作為 raw alpha signal → 餵入 portfolio optimizer 或 ranking model |
| **Feature Source** | 完整 OHLCV 預測軌跡作為下游 ML 特徵（predicted volatility = pred_high - pred_low, predicted return = pred_close/current_close - 1）|
| **Regime Detection** | 比對預測 vs 當前價格模式 → 分類市場狀態 |
| **Direct Signal** | 預測後直接產生 long/short action → 餵入現有 cost model + execution engine |

### 具體整合路徑

1. 包裝 `KronosPredictor` 為 signal source，在 data pipeline 中定期呼叫
2. 將預測輸出映射為策略期望的 signal 格式
3. 透過 `BaseStrategy.on_bar()` 的 context 傳入（例如作為 bar data 的額外欄位）
4. 現有 cost model、position management、execution engine 維持不變

### 關鍵考量

- 模型是 **stateless** — 每次獨立處理 window，無跨次呼叫狀態
- 正規化由 `KronosPredictor` 內部處理
- 多標的場景用 `predict_batch()` 效率顯著提升
- `sample_count > 1` 時輸出為概率性預測（多路徑平均）

---

## 7. 延伸應用

### A. 波動率預測（Volatility Forecasting）

透過預測 K 線的 High-Low 價差估算未來波動率，適用於：
- 期權交易（Options Trading）
- 動態部位管理（Dynamic Position Sizing）

### B. 強化學習的特徵提取器

將 Kronos Transformer 倒數第二層輸出作為 Embedding → 高維市場狀態 → RL Agent 的 State 輸入。比原始價格更具表達力。

### C. 壓力測試與合成數據

利用生成能力產生大量「虛擬但符合統計特性的 K 線」：
- 測試策略在極端市場（閃崩）下的表現
- 擴充訓練樣本，減少過擬合

---

## 8. 優勢與限制

### 優勢

1. **Domain-specific**: 原生處理 OHLCV K 線結構，保留欄位間關係（high >= open, close）
2. **階層式 Tokenization**: 粗+細雙層 Token 同時捕捉結構與細節
3. **45+ 交易所預訓練**: 強 zero-shot 泛化，跨市場/品種/頻率
4. **完整 Pipeline**: 從原始數據 → 預測 → 回測，含 Qlib 和 CSV 兩條路徑
5. **概率性預測**: 多路徑 sampling + temperature/top_p 控制 → 不確定性估計
6. **小模型可用**: base 102M params，消費級硬體可推理
7. **HuggingFace 整合**: 標準 `from_pretrained` 載入

### 限制

1. **不是交易系統**: 純預測模型，無 position/risk management、order execution、portfolio optimization
2. **Context length 限制**: small/base 僅 512 bars（daily ≈ 2 年，5min ≈ 2 交易日）
3. **無 streaming inference**: 無增量推理，每次處理完整 window
4. **Volume 預測品質較低**: 成交量動態與價格本質不同
5. **無校準的不確定性量化**: 僅靠 sampling，無 confidence interval 或 calibrated uncertainty
6. **Autoregressive 推理慢**: 120 bars 需 120 sequential forward passes，無 KV-cache 優化
7. **無跨資產相關性建模**: 每條序列獨立預測，無 cross-asset attention
8. **僅接受 OHLCV**: 不支援衍生特徵（RSI, MACD）、order book、基本面、另類數據

---

## 9. 量化研究的專業提醒

| 陷阱 | 說明 |
|---|---|
| **Data Snooping Bias** | 避免在同一段歷史數據上反覆測試閾值 θ |
| **Stationarity** | 金融數據非平穩。2021 多頭表現好 ≠ 能預測 2022 熊市。微調應包含完整牛熊週期 |
| **Execution Cost** | 高頻預測可能導致頻繁交易 → 回測必須精確計算手續費與滑點，否則微小 Alpha 被成本吞噬 |
| **Survivorship Bias** | 個股分析需包含已下市公司，否則回測結果虛高 |
| **Look-ahead Bias** | 微調時必須嚴格區分 train/val/test 時間範圍 |

---

## 10. Repo 內建範例

| 範例 | 路徑 | 說明 |
|---|---|---|
| 基礎預測 | `examples/prediction_example.py` | 單序列，400-bar lookback, 120-bar forecast, 5min A 股 |
| Batch 預測 | `examples/prediction_batch_example.py` | 5 條平行序列 |
| 無 Volume | `examples/prediction_wo_vol_example.py` | 僅 OHLC |
| A 股即時 | `examples/prediction_cn_markets_day.py` | akshare 下載 + 預測 + 漲跌幅限制 + 儲存 |
