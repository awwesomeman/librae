"""Point-in-time third-party factor enrichment for sim/live polling.

Run with:
    python -m examples.custom_data_provider

``CompositeBarFetcher`` is an example owned by strategy/user code, not an
engine service. Inject an instance as ``LiveTrader(adapter=provider)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
from librae import AccountConfig, Backtest, CostModel, ExecutionPolicy, OrderIntent, RunConfig
from librae.core.strategy import Context, Strategy
from librae.live.engine import LiveTrader
from librae.live.state import MemoryLiveStateStore

BarFetcher = Callable[..., pd.DataFrame]
FactorFetcher = Callable[[str], pd.DataFrame]


@dataclass(frozen=True)
class CompositeBarFetcher:
    """Add the latest factor that was available at each bar timestamp."""

    price_fetcher: BarFetcher
    factor_fetcher: FactorFetcher
    max_factor_age: pd.Timedelta

    def __call__(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        *,
        drop_incomplete: bool = False,
    ) -> pd.DataFrame:
        bars = self.price_fetcher(
            symbol,
            timeframe,
            limit,
            drop_incomplete=drop_incomplete,
        ).copy()
        factors = self.factor_fetcher(symbol).copy()
        required = {"available_at", "factor_score"}
        missing = required - set(factors.columns)
        if missing:
            raise ValueError(f"factor data missing columns: {sorted(missing)}")

        bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
        factors["available_at"] = pd.to_datetime(factors["available_at"], utc=True)
        return pd.merge_asof(
            bars.sort_values("ts"),
            factors.sort_values("available_at"),
            left_on="ts",
            right_on="available_at",
            direction="backward",
            tolerance=self.max_factor_age,
        )


def require_factor_and_add_signals(history: pd.DataFrame) -> pd.DataFrame:
    """Fail closed when the current snapshot has no fresh required factor."""
    if history.empty or pd.isna(history.iloc[-1].get("factor_score")):
        raise ValueError("required factor_score is missing or stale")
    featured = history.copy()
    featured["entry_signal"] = featured["factor_score"] > 0.5
    featured["exit_signal"] = featured["factor_score"] < 0
    return featured


class FactorThresholdStrategy(Strategy):
    """Use the same enriched feature in backtest and shadow simulation."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.positions.get(ctx.symbol):
            return []
        if bool(ctx.bar["entry_signal"]):
            return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
        return []


def _demo_prices(
    _symbol: str,
    _timeframe: str,
    limit: int,
    *,
    drop_incomplete: bool = False,
) -> pd.DataFrame:
    del drop_incomplete
    ts = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=limit, freq="h")
    return pd.DataFrame(
        {
            "ts": ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000.0,
        }
    )


def _demo_factors(_symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "available_at": pd.to_datetime(
                ["2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z"],
                utc=True,
            ),
            "factor_score": [0.25, 0.75],
        }
    )


def main() -> None:
    provider = CompositeBarFetcher(
        price_fetcher=_demo_prices,
        factor_fetcher=_demo_factors,
        max_factor_age=pd.Timedelta("2h"),
    )
    enriched = provider("BTCUSDT", "1h", 5, drop_incomplete=True)
    featured = require_factor_and_add_signals(enriched)

    backtest_data = featured.rename(columns={"ts": "datetime"}).assign(symbol="BTCUSDT")
    backtest_data = backtest_data.set_index(["symbol", "datetime"])
    backtest = Backtest(
        data=backtest_data,
        strategy=FactorThresholdStrategy(),
        cost_model=CostModel.zero(),
        data_source="demo",
    )
    backtest_result = backtest.run()

    config = RunConfig(
        strategy_name="custom_factor_demo",
        symbols=["BTCUSDT"],
        timeframe="H1",
        market="crypto",
        data_source="binance_spot",
        accounts={"default": AccountConfig(currency="USDT", initial_cash=100_000.0)},
        mode="sim",
        execution=ExecutionPolicy(
            max_bar_volume_participation_rate=None,
            warmup_periods=5,
        ),
        poll_seconds=0,
        no_db=True,
    )
    trader = LiveTrader(
        FactorThresholdStrategy(),
        require_factor_and_add_signals,
        config=config,
        adapter=provider,
        cost_model=CostModel.zero(),
        notifier=None,
        on_bar=None,
        on_order_event=None,
        on_ohlcv=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        warmup_fetcher=None,
        state_store=MemoryLiveStateStore(),
        clock=lambda: datetime(2026, 1, 1, 6, tzinfo=UTC),
    )
    trader.run(max_iterations=1)

    print(
        f"backtest_trades={len(backtest_result.trades)} "
        f"sim_last_bar={enriched['ts'].iloc[-1].isoformat()}"
    )


if __name__ == "__main__":
    main()
