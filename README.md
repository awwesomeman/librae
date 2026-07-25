# librae

量化回測與即時交易引擎。`db/`、`brokers/`、`notifications/`、`orchestration/` 是 db 落地、broker 下單、通知、CLI 組裝的擴充範例——都是選用的，`librae` 本身不強制依賴任何一個。

---

## Quick Start（本機）

支援 Python 3.12 / 3.13 / 3.14（CI 三個版本都跑，見 `.github/workflows/core-tests.yml`）。

```bash
git clone git@github-librae:awwesomeman/librae.git
cd librae
uv sync --extra test --extra dev   # 開發/測試用；要跑 brokers/ 的 shioaji/ib_async 才需要加 --extra tw-live/--extra us-live
git config core.hooksPath .githooks   # commit 前跑 ruff check + format --check
```

之後所有指令都透過 `uv run` 執行（例如 `uv run pytest tests/ -q`），或 `source .venv/bin/activate` 後直接跑。

---

## 回測引擎 (librae)

量化回測與即時交易引擎。提供策略執行、持倉管理、成本模擬、績效計算的完整框架，**回測、模擬、實盤共用同一份策略，零修改**。

```python
from librae import Backtest, BaseStrategy, Action, Context, RunConfig

class MyStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.symbol):
            if ctx.bar.get("exit_signal"):
                return [Action(type="close", symbol=ctx.symbol)]
            return []
        if ctx.bar.get("entry_signal"):
            return [Action(type="long", symbol=ctx.symbol)]
        return []

df = fetch_and_prepare(symbol, months)          # 你的 ETL，資料格式見下方連結
bt = Backtest(data=df, strategy=MyStrategy(), cfg=cfg)
bt.run()
output = bt.build_output()                      # BacktestOutput
```

引擎的目錄結構、依賴方向、風控/保證金/對帳/staleness 偵測細節、核心型別、設計決策、Config API 完整說明見 [`architecture.md`「回測引擎設計」](architecture.md#回測引擎設計librae)。

---

## 擴充範例

引擎本身不 import 這些套件——`LiveTrader` 用建構子參數（`adapter`/`order_adapter`/`cost_model`/`notifier`）注入，或 `cfg.no_db=True` 時完全跳過，未注入時才 lazy import 以下預設實作。

| 目錄 | 對應注入點 | 說明 |
|---|---|---|
| `db/` | db 寫入 callback | TimescaleDB 讀寫；schema 見 `db/timescale_init.sql`，範例資料見 `db/seed_fake_data.sql` |
| `brokers/` | `adapter` / `order_adapter` | Shioaji（台灣期貨）、CCXT（crypto）、IBKR adapter |
| `notifications/` | `notifier` | Telegram 通知 |
| `orchestration/` | — | `cli.py`：`RunConfig` 建構 + CLI 參數合併，組裝上面三者注入引擎的參考寫法 |

自己接資料庫/broker/通知只需實作對應的 duck-typed 介面，不需要用這幾個套件。

---

## 常用指令

| 指令 | 說明 |
|------|------|
| `pytest tests/ -q` | 跑測試 |
| `ruff check .` | Lint（範圍見 `pyproject.toml` `[tool.ruff]`） |
| `ruff format .` | 格式化 |

---

## 設定檔總覽

| 檔案 | 設定什麼 | 是否進 git |
|------|---------|-----------|
| `librae/config/markets.yaml` | 市場成本 + 保證金參數（也可外部注入，繞過此檔，見 `get_market(markets=)`） | yes |
| `librae/config/symbols.yaml` | symbol → market/data_source 對應 | yes |
| `db/timescale_init.sql` | DB schema（`db/` 擴充範例用） | yes |

---

## 相關文件

- [「回測引擎 (librae)」](#回測引擎-librae)（本文件）— 引擎架構、API、類型系統
- [`architecture.md`](architecture.md) — 系統分層、命名慣例
- [`docs/decisions/`](docs/decisions/) — 架構決策記錄
- [`docs/plans/`](docs/plans/) — 執行計劃
- [`docs/learnings/ERRORS.md`](docs/learnings/ERRORS.md) — 除錯記錄（症狀/根因/修法/預防）
