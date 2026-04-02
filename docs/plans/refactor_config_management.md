# Config Management 重構計畫

> 狀態：planning
> 範圍：config, telegram, notification
> 建立日期：2026-03-31
> 最後更新：2026-03-31
> 備註：2026-03-31 批判檢視 5 點已整合（見 §8）

---

## 1) 背景

### 目前的問題

專案有 **4 種設定載入方式**，各自獨立：

| # | 設定類型 | 載入方式 | 預設值位置 | Typed? |
|---|---------|---------|-----------|--------|
| A | 市場成本 | `markets.yaml` → `MarketConfig` dataclass | YAML | ✅ frozen dataclass |
| B | 策略參數 | `config.yaml` → argparse defaults | argparse + YAML 混合 | ❌ raw Namespace |
| C | Telegram | env var → `TelegramConfig` field_factory | dataclass field_factory | ✅ dataclass |
| D | 券商憑證 | env var → `CredentialConfig.from_env()` | dataclass | ✅ frozen dataclass |

**具體痛點：**

1. **Telegram 通知只有全域開關** — `TELEGRAM_ENABLED` env var，無法按策略控制
2. **`send_status()` / `send_heartbeat_timeout()` 已實作但未接入** — 沒有設定機制觸發
3. **使用者不知道有哪些參數** — 設定散落在 `cli.py`、`wiring.py`、`telegram.py`
4. **TelegramConfig 混了 secrets 和行為設定** — bot_token（secret）和 enabled（行為）用同一個 pattern
5. **C 和 D 的 env var 載入 pattern 不同** — field_factory vs `from_env()`

### 設計原則

- **可公開的放 YAML，不能公開的放 env var**
- **預設值在 Python dataclass** — YAML 是「文件範本」，dataclass 是真正的 default
- **Fail fast** — 啟動時轉 typed dataclass，打錯字立即報錯
- **不加新依賴** — 用標準 dataclass，不引入 pydantic-settings

---

## 2) 目標架構

### 設定分類

```
YAML 檔案 → typed dataclass     適用：市場參數、策略行為、通知偏好
env var   → from_env()          適用：secrets（token, DSN, 密碼）
CLI args  → argparse            適用：runtime flags（--mode, --symbol, --dry-run）
```

### 合併優先順序

```
strategies/*/config.yaml           ← 策略只寫差異
  ↓ flat keys override
CLI args (--symbol, --mode)        ← 最高優先（僅 runtime flags）
```

預設值來源：flat keys → `base_parser()` argparse defaults，structured keys → dataclass defaults。

### TelegramConfig 拆分

```
Before（混在一起）:
  TelegramConfig ← env var → { enabled, bot_token, chat_id }

After（分離）:
  TelegramCredentials ← env var → { bot_token, chat_id }     ← secrets，用 from_env()
  TelegramConfig      ← YAML   → { enabled, notifications }  ← 行為，用 from_dict()
```

---

## 3) 改動清單

### 3.1 ~~新增 `librae/config/defaults.yaml`~~ — 已刪除

> 決定不使用獨立的 defaults.yaml 參考檔。預設值的唯一來源：
> - flat keys → `base_parser()` 的 argparse defaults（查 `--help`）
> - structured keys → dataclass defaults（`librae/config/notification.py`）
>
> 策略 config.yaml 頂部加註解指路即可。

### 3.2 新增 `librae/config/notification.py` — typed dataclass

```python
@dataclass
class StatusConfig:
    enabled: bool = False
    interval_bars: int = 12

@dataclass
class NotificationConfig:
    signal: bool = True
    startup: bool = True
    error: bool = True
    status: StatusConfig = field(default_factory=StatusConfig)

    @classmethod
    def from_dict(cls, d: dict) -> NotificationConfig: ...

@dataclass
class TelegramConfig:
    enabled: bool = False
    chat_id: str = ""
    notifications: NotificationConfig = field(default_factory=NotificationConfig)

    @classmethod
    def from_dict(cls, d: dict) -> TelegramConfig: ...
```

### 3.3 改 `librae/notifications/telegram.py` — secrets 改用 from_env()

```python
# Before: TelegramConfig 用 field_factory 讀 env var（混 secrets + 行為）
# After:  TelegramCredentials(CredentialConfig) 只管 secrets

@dataclass
class TelegramCredentials(CredentialConfig):
    bot_token: str = ""
    chat_id: str = ""
    # 載入：TelegramCredentials.from_env("TELEGRAM")

class TelegramAdapter:
    def __init__(
        self,
        config: TelegramConfig | None = None,        # 行為設定（from YAML）
        credentials: TelegramCredentials | None = None, # secrets（from env）
    ) -> None: ...
```

各 `send_*` 方法內部檢查 `NotificationConfig` flag：
- `send_signal()` → check `config.notifications.signal`
- `send_startup()` / `send_shutdown()` → check `config.notifications.startup`
- `send_alert()` → check `config.notifications.error`
- `send_status()` → check `config.notifications.status.enabled`

### 3.4 改 `librae/cli.py` — 三層合併 + structured keys 分離

```python
STRUCTURED_KEYS = {"telegram"}

def load_defaults() -> dict:
    """Load librae/config/defaults.yaml."""
    ...

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge. override 覆蓋 base，nested dict 遞迴合併。"""
    ...

def parse_with_config(parser: argparse.ArgumentParser) -> argparse.Namespace:
    # 1. Load defaults.yaml
    # 2. Deep merge strategy config.yaml (if --config)
    # 3. Flat keys → argparse defaults, structured keys → args attribute
    # 4. Parse CLI args (highest priority for flat keys)
    ...
```

### 3.5 改 `librae/live/wiring.py` — 讀取 TelegramConfig

```python
def build_live_trader(
    *,
    ...,
    telegram_config: dict | None = None,  # 從 args.telegram 傳入
) -> LiveTrader:
    config = TelegramConfig.from_dict(telegram_config or {})
    credentials = TelegramCredentials.from_env("TELEGRAM")
    telegram = TelegramAdapter(config=config, credentials=credentials)
    ...
```

### 3.6 改 `librae/live/engine.py` — 條件化通知 + send_status 接入

- `_notify()` 檢查 `NotificationConfig` flag
- 新增 `_status_bar_count` 計數器
- 每 `status.interval_bars` 根 bar 呼叫 `send_status()`
- startup/shutdown/error 依 flag 決定

### 3.7 改 `strategies/*/run.py` — 傳遞 telegram config

```python
def run_sim(args: argparse.Namespace) -> None:
    trader = build_live_trader(
        ...,
        telegram_config=getattr(args, "telegram", None),
    )
```

### 3.8 更新 `strategies/*/config.yaml`

精簡為差異 + telegram 區塊：

```yaml
# strategies/trendpullback_m5/config.yaml
strategy: trendpullback_m5
poll_interval: 30
max_hold_bars: 24
months: 1

telegram:
  enabled: true
  notifications:
    status:
      enabled: true
```

### 3.9 測試

- `tests/config/test_notification.py` — NotificationConfig / TelegramConfig from_dict + deep merge
- `tests/config/test_cli.py` — 三層合併邏輯
- `tests/notifications/test_telegram_adapter.py` — 條件化 send_*
- `tests/engine/test_live_runner.py` — send_status 定期觸發

---

## 4) 不動的部分

| 項目 | 原因 |
|------|------|
| `MarketConfig` + `markets.yaml` | 已經是最佳 pattern，不需改 |
| `CredentialConfig.from_env()` | secrets 載入 pattern 已經很好 |
| 策略參數 typed dataclass | 目前只有 2 個策略，argparse Namespace 夠用。等策略 >5 再考慮 |
| 訊息格式自訂 | 只有一個使用者，hardcoded 格式夠用 |
| Heartbeat 外部監控腳本 | Phase 4 再做 |

---

## 5) 統一後的設定架構總覽

```
librae/config/
├── markets.yaml              ← 市場定義（不動）
├── market_config.py          ← MarketConfig dataclass（不動）
└── notification.py           ← TelegramConfig + NotificationConfig（新增）

librae/notifications/
└── telegram.py               ← TelegramAdapter + TelegramCredentials

brokers/
└── base.py                   ← CredentialConfig.from_env()（不動）

strategies/*/
└── config.yaml               ← 只寫差異

deploy/
└── .env                      ← secrets only: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ...
```

**設定載入流程（統一後）：**

```
啟動
├── cli.py: parse strategy config.yaml → separate structured keys → parse CLI
│   ├── flat keys → argparse defaults → Namespace（symbol, mode, poll_interval, ...）
│   └── structured keys → args.telegram dict
│
├── wiring.py: TelegramConfig.from_dict(args.telegram)     ← 行為
│              TelegramCredentials.from_env("TELEGRAM")     ← secrets
│              TelegramAdapter(config, credentials)
│
├── market_config.py: MarketConfig from markets.yaml        ← 不動
│
└── CryptoCredentials.from_env("CRYPTO")                    ← 不動
```

---

## 6) 驗證計畫

1. `pytest tests/config/ tests/notifications/ tests/engine/test_live_runner.py -v` — 新增 + 既有測試全過
2. `pytest --tb=short -q` — 全部通過
3. 本機 `python -m strategies.trendpullback_m5.run --mode sim --no-db` — 驗證啟動/停止通知
4. config.yaml 設 `signal: false` → 確認不發信號通知
5. config.yaml 設 `status.enabled: true, interval_bars: 3` → 確認每 3 根 bar 收到摘要
6. 不設 `--config` → argparse + dataclass 預設值生效 → telegram disabled

---

## 7) 預期使用方式

### 查有哪些參數？

```bash
cat librae/config/defaults.yaml   # 有完整註解
```

### 策略 config.yaml（只寫差異）

```yaml
# strategies/trendpullback_m5/config.yaml
strategy: trendpullback_m5
poll_interval: 30
max_hold_bars: 24
months: 1

telegram:
  enabled: true
  notifications:
    status:
      enabled: true
```

### 跑 sim

```bash
# 本機
source .env.local
python -m strategies.trendpullback_m5.run --mode sim

# Docker
./deploy/sim_start.sh trendpullback_m5 BTCUSDT 30
```

### 臨時覆蓋

```bash
python -m strategies.trendpullback_m5.run --mode sim --symbol ETHUSDT
```

### 關掉某策略的通知

```yaml
telegram:
  enabled: false
```

---

## 8) 批判檢視修正記錄

| # | 問題 | 修正 |
|---|------|------|
| F1 | 通知 key 命名 `on_trade`/`on_alert` 語義模糊、跟 engine callback 衝突 | 用 `signal`/`startup`/`error`/`status`，跟 TelegramAdapter method 名對齊 |
| F2 | `defaults.yaml` 放根目錄還是 `librae/config/`？ | 放 `librae/config/`（對齊 `markets.yaml`，是框架內部預設值，非使用者範本） |
| F3 | Status update 週期用固定時間還是 bar 數？ | 用 `interval_bars`（自動適應不同 timeframe：M5×12=1h, H1×24=1d） |
| F4 | `TelegramCredentials.from_env("TELEGRAM")` 的 field mapping | `bot_token` → `TELEGRAM_BOT_TOKEN`, `chat_id` → `TELEGRAM_CHAT_ID`（符合 CredentialConfig 慣例） |
| F5 | engine.py 不該直接讀 config | wiring.py 負責建構 TelegramAdapter（依賴注入），engine.py 只用注入好的 adapter |
