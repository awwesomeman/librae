"""_bind_market_data_source() merges funding_rate onto perpetual OHLCV bars."""

from __future__ import annotations

import pandas as pd
from librae.config.symbols import SymbolInfo
from librae.live.engine import _bind_market_data_source


def _instrument(instrument_type: str) -> SymbolInfo:
    return SymbolInfo(
        symbol="BTC-PERP",
        market="crypto",
        data_source="binance",
        instrument_type=instrument_type,
        multiplier=1.0,
        data_adapter="crypto",
        venue_symbol="BTC/USDT:USDT",
        currency="USDT",
    )


class _FakeCryptoAdapter:
    def __init__(self) -> None:
        self.funding_calls: list[tuple[str, int]] = []

    def fetch_ohlcv(self, symbol, timeframe, limit, *, drop_incomplete=False, **_kwargs):
        return pd.DataFrame(
            {
                "ts": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z"], utc=True),
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [10.0, 20.0],
            }
        )

    funding_ts = "2026-01-01T08:00:00Z"

    def fetch_funding_rate_history(self, symbol, limit=100, *, since=None):
        self.funding_calls.append((symbol, limit))
        return pd.DataFrame(
            {
                "ts": pd.to_datetime([self.funding_ts], utc=True),
                "funding_rate": [0.0001],
            }
        )


def test_perpetual_instrument_gets_funding_rate_merged_onto_bars():
    adapter = _FakeCryptoAdapter()
    fetcher = _bind_market_data_source(adapter, _instrument("contract_perpetual"))

    bars = fetcher("BTC-PERP", "8h", 2)

    assert bars["funding_rate"].isna().tolist() == [True, False]
    assert bars["funding_rate"].iloc[1] == 0.0001
    assert adapter.funding_calls == [("BTC/USDT:USDT", 2)]


def test_spot_instrument_does_not_fetch_funding():
    adapter = _FakeCryptoAdapter()
    fetcher = _bind_market_data_source(adapter, _instrument("spot"))

    bars = fetcher("BTC-PERP", "8h", 2)

    assert "funding_rate" not in bars.columns
    assert adapter.funding_calls == []


class _JitteredFundingAdapter(_FakeCryptoAdapter):
    """Binance stamps fundingTime a few milliseconds past the settlement hour."""

    funding_ts = "2026-01-01T08:00:00.003Z"


class _EarlyJitteredFundingAdapter(_FakeCryptoAdapter):
    funding_ts = "2026-01-01T07:59:59.997Z"


def test_millisecond_jittered_funding_still_merges_onto_its_bar():
    adapter = _JitteredFundingAdapter()
    fetcher = _bind_market_data_source(adapter, _instrument("contract_perpetual"))

    bars = fetcher("BTC-PERP", "8h", 2)

    assert (
        bars["ts"].tolist()
        == pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z"], utc=True).tolist()
    )
    assert bars["funding_rate"].isna().tolist() == [True, False]
    assert bars["funding_rate"].iloc[1] == 0.0001


def test_funding_settling_just_before_the_bar_merges_onto_that_bar():
    adapter = _EarlyJitteredFundingAdapter()
    fetcher = _bind_market_data_source(adapter, _instrument("contract_perpetual"))

    bars = fetcher("BTC-PERP", "8h", 2)

    assert bars["funding_rate"].isna().tolist() == [True, False]


def test_funding_far_from_any_bar_is_not_attributed():
    class _StaleFundingAdapter(_FakeCryptoAdapter):
        funding_ts = "2026-01-01T07:00:00Z"

    adapter = _StaleFundingAdapter()
    fetcher = _bind_market_data_source(adapter, _instrument("contract_perpetual"))

    bars = fetcher("BTC-PERP", "8h", 2)

    assert bars["funding_rate"].isna().all()
