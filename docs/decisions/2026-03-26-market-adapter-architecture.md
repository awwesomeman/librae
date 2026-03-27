# 2026-03-26 — MarketAdapter / MarketHub 抽象層設計

> 狀態：accepted

## 背景

執行層有三個市場（Crypto/US/TW），工具各異（CCXT / ib_insync / Shioaji）。
若 signal_engine 直接依賴各工具 API，換市場或換工具就得大改邏輯。

## 決策

採用 **Adapter + Hub 模式**，由 MarketHub 統一分派，各市場自己封裝細節。

## 架構

```python
class MarketAdapter:
    """每個市場一個 adapter，同時負責資料和執行"""

    def fetch_ohlcv(self, symbol, timeframe, limit) -> pd.DataFrame:
        """統一 OHLCV 格式：columns=[ts, open, high, low, close, volume]"""
        raise NotImplementedError

    def place_order(self, signal: dict) -> dict:
        """統一下單介面"""
        raise NotImplementedError

    def get_position(self, symbol) -> dict:
        """查詢持倉"""
        raise NotImplementedError

class CryptoAdapter(MarketAdapter):  # 封裝 CCXT
class IBAdapter(MarketAdapter):      # 封裝 ib_insync
class TWSAdapter(MarketAdapter):     # 封裝 Shioaji

class MarketHub:
    """統一入口，dispatch 到對應 adapter"""

    def __init__(self):
        self.adapters = {
            'CRYPTO': CryptoAdapter(...),
            'US':     IBAdapter(...),
            'TW':     TWSAdapter(...),
        }

    def fetch_ohlcv(self, market, symbol, timeframe, limit):
        return self.adapters[market].fetch_ohlcv(symbol, timeframe, limit)

    def execute_signal(self, signal):
        return self.adapters[signal['market']].place_order(signal)
```

## 設計原則

- **MarketAdapter** 定義三個介面：資料拉取、下單、查倉位
- **MarketHub** 統一 dispatch：不只管執行，也管資料拉取
- **OHLCV 格式由 adapter 標準化**，signal_engine 只看 DataFrame，完全不知道資料來源
- **加新市場只需加一個 adapter**，MarketHub 不改（開放封閉原則）

## 好處

| 優點 | 說明 |
|------|------|
| signal_engine 解耦 | 只吃標準 DataFrame，不碰任何市場細節 |
| 可擴展 | 加台期：實作 TWSAdapter，MarketHub 加一行 |
| 測試友善 | live 和回測都可 mock adapter（注入假資料） |

## 與 platform-architecture.md 的關係

- `platform-architecture.md` 定義「選用哪些工具」（CCXT/ib_insync/Shioaji）
- 本文件定義「如何封裝這些工具」（Adapter 抽象介面）
- 兩者互補，本文件是執行層的實作規範

## 不採用的方案

- 在 signal_engine 直接 import CCXT/Shioaji：耦合度高，換市場等於改策略邏輯
- 只做 Hub 不做 Adapter 介面：缺少 contract 約束，容易出現不一致的 API
