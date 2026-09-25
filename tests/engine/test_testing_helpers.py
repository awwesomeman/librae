"""Tests for third-party integration conformance helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from librae.live.executor import OrderRequest
from librae.testing import (
    normalize_broker_report,
    validate_bar_data,
    validate_feature_causality,
    validate_order_adapter,
)


def _request() -> OrderRequest:
    return OrderRequest(
        client_order_id="client-1",
        symbol="BTCUSDT",
        venue_symbol="BTC/USDT",
        side="buy",
        quantity=1.0,
        order_type="market",
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_validate_order_adapter_accepts_complete_shape() -> None:
    validate_order_adapter(MagicMock())


def test_validate_order_adapter_lists_missing_methods() -> None:
    class IncompleteAdapter:
        def place_order(self, signal: dict) -> dict:
            return signal

    with pytest.raises(TypeError, match=r"prepare_order.*get_position"):
        validate_order_adapter(IncompleteAdapter())


def test_normalize_broker_report_uses_live_contract() -> None:
    report = normalize_broker_report(
        _request(),
        {
            "id": "order-1",
            "status": "filled",
            "amount": 1.0,
            "filled": 1.0,
            "average": 100.0,
            "commission": 0.1,
            "executed_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        },
    )

    assert report.order_id == "order-1"
    assert report.status == "filled"


def test_normalize_broker_report_applies_adapter_client_id_mapping() -> None:
    class CompactIdAdapter:
        @staticmethod
        def broker_client_order_id(client_order_id: str) -> str:
            return client_order_id[:3]

    raw = {"id": "order-1", "clientOrderId": "cli", "status": "submitted", "amount": 1.0}

    with pytest.raises(ValueError, match="client order id"):
        normalize_broker_report(_request(), raw)
    report = normalize_broker_report(_request(), raw, adapter=CompactIdAdapter())

    assert report.client_order_id == "client-1"


def test_normalize_broker_report_rejects_invented_fill_facts() -> None:
    with pytest.raises(ValueError, match="commission"):
        normalize_broker_report(
            _request(),
            {
                "id": "order-1",
                "status": "filled",
                "amount": 1.0,
                "filled": 1.0,
                "average": 100.0,
                "executed_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            },
        )


def test_validate_bar_data_accepts_canonical_frame() -> None:
    frame = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 11.0],
        }
    )

    validate_bar_data(frame)


def test_validate_bar_data_rejects_naive_timestamps() -> None:
    frame = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=1, freq="h"),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [10.0],
        }
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        validate_bar_data(frame)


def _bars(periods: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, periods))
    index = pd.date_range("2026-01-01", periods=periods, freq="h", tz="UTC")
    return pd.DataFrame({"close": close, "volume": rng.uniform(1, 10, periods)}, index=index)


def _panel(periods: int = 120) -> pd.DataFrame:
    return pd.concat(
        {"AAA": _bars(periods), "BBB": _bars(periods) * 2},
        names=["symbol", "datetime"],
    )


def _with(column: Callable[[pd.Series], pd.Series]) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def feature_fn(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["feature"] = column(out["close"])
        return out

    return feature_fn


@pytest.mark.parametrize(
    "column",
    [
        lambda close: close.rolling(10).mean(),
        lambda close: close.ewm(span=10).mean(),
        lambda close: close.pct_change(),
        lambda close: close.shift(1),
        lambda close: (close > close.rolling(5).mean()).astype(object),
    ],
    ids=["rolling", "ewm", "pct_change", "shift_back", "object"],
)
def test_validate_feature_causality_accepts_causal_features(
    column: Callable[[pd.Series], pd.Series],
) -> None:
    validate_feature_causality(_with(column), _bars())


@pytest.mark.parametrize(
    "column",
    [
        lambda close: close.shift(-1),
        lambda close: close.rolling(10).mean().bfill(),
        lambda close: close.rolling(5, center=True).mean(),
        lambda close: (close - close.mean()) / close.std(),
        lambda close: (close > close.mean()).map({True: "hi", False: "lo"}),
    ],
    ids=["shift_forward", "bfill", "centered", "zscore", "non_numeric"],
)
def test_validate_feature_causality_rejects_look_ahead(
    column: Callable[[pd.Series], pd.Series],
) -> None:
    with pytest.raises(ValueError, match=r"'feature'.*cut at"):
        validate_feature_causality(_with(column), _bars())


def test_validate_feature_causality_names_timestamp_and_only_leaking_columns() -> None:
    def feature_fn(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["causal"] = out["close"].shift(1)
        out["future"] = out["close"].shift(-1)
        return out

    bars = _bars()
    with pytest.raises(ValueError) as caught:
        validate_feature_causality(feature_fn, bars, cuts=1)

    message = str(caught.value)
    assert "['future']" in message
    assert "causal" not in message
    assert str(bars.index[-2]) in message


def test_validate_feature_causality_rejects_rows_or_columns_that_depend_on_later_bars() -> None:
    def drops_rows(df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(future=df["close"].shift(-1)).dropna()

    def adds_column(df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(late=1.0) if len(df) == 120 else df.copy()

    with pytest.raises(ValueError, match="rows"):
        validate_feature_causality(drops_rows, _bars())
    with pytest.raises(ValueError, match=r"columns.*'late'"):
        validate_feature_causality(adds_column, _bars())


def test_validate_feature_causality_cuts_panel_symbols_at_same_timestamp() -> None:
    def per_symbol(shift: int) -> Callable[[pd.DataFrame], pd.DataFrame]:
        def feature_fn(df: pd.DataFrame) -> pd.DataFrame:
            out = df.copy()
            out["feature"] = out.groupby(level="symbol")["close"].shift(shift)
            return out

        return feature_fn

    validate_feature_causality(per_symbol(1), _panel())
    with pytest.raises(ValueError, match=r"\('AAA', Timestamp"):
        validate_feature_causality(per_symbol(-1), _panel())


def test_validate_feature_causality_does_not_mutate_bars() -> None:
    def mutating(df: pd.DataFrame) -> pd.DataFrame:
        df["close"] = df["close"].rolling(3).mean()
        return df

    bars = _bars()
    original = bars.copy()

    validate_feature_causality(mutating, bars)

    pd.testing.assert_frame_equal(bars, original)


@pytest.mark.parametrize(
    "bars,cuts,match",
    [
        (_bars(6), 5, "at least 7 timestamps"),
        (_bars(), 0, "cuts must be positive"),
        (_bars().reset_index(drop=True), 5, "DatetimeIndex"),
        (pd.concat([_bars(), _bars()]), 5, "unique"),
    ],
    ids=["too_short", "no_cuts", "bad_index", "duplicates"],
)
def test_validate_feature_causality_rejects_unusable_input(
    bars: pd.DataFrame,
    cuts: int,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        validate_feature_causality(_with(lambda close: close), bars, cuts=cuts)


def test_validate_feature_causality_requires_frame_output() -> None:
    with pytest.raises(TypeError, match="DataFrame"):
        validate_feature_causality(lambda df: df["close"], _bars())  # type: ignore[arg-type,return-value]


def test_validate_feature_causality_requires_unique_output_columns() -> None:
    def feature_fn(df: pd.DataFrame) -> pd.DataFrame:
        return pd.concat([df, df[["close"]]], axis=1)

    with pytest.raises(ValueError, match=r"unique.*\['close'\]"):
        validate_feature_causality(feature_fn, _bars())
