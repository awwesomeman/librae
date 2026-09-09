"""Simulated time-in-force lifetime and fill semantics.

librae is a bar-based engine, so a TIF reduces to two questions an OHLCV
feed can actually answer: how many events the order stays eligible on, and
whether a short fill on an eligible event counts. Queue position and book
depth are deliberately not modeled.
"""

from __future__ import annotations

import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import validate_strategy_decision
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import OrderIntent, Strategy


def _panel(periods: int = 6, volume: float = 100.0) -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [["X"], pd.date_range("2025-01-01", periods=periods, freq="h", tz="UTC")],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": [100.0] * periods,
            "high": [101.0] * periods,
            "low": [99.0] * periods,
            "close": [100.0] * periods,
            "volume": [volume] * periods,
        },
        index=index,
    )


def _submit_once(intent: OrderIntent) -> Strategy:
    class SubmitOnce(Strategy):
        def on_bar(self, ctx):
            return [intent] if ctx.period_index == 0 else []

    return SubmitOnce()


def _run(intent: OrderIntent, *, participation: float | None = 0.1, periods: int = 6):
    return Backtest(
        _panel(periods=periods),
        _submit_once(intent),
        initial_balance=100_000.0,
        cost_model=CostModel.zero(),
        data_source="test",
        execution=ExecutionPolicy(max_bar_volume_participation_rate=participation),
    ).run()


def _skip_reasons(result) -> list[str]:
    return [
        str(event.detail.get("reason"))
        for event in result.runtime_events
        if event.event_type == "decision_skipped"
    ]


def _validate(decision: list[OrderIntent], *, bars: dict | None = None) -> None:
    validate_strategy_decision(
        decision,
        {"X", "Y"},
        primary_symbol="X",
        bars=bars if bars is not None else {"X": {"close": 100.0}, "Y": {"close": 100.0}},
        positions={},
    )


class TestUnsupportedCombinationsFailAtPreflight:
    """Preflight rejects what this engine cannot express, not what one venue
    dislikes. A backtest is broker-neutral: venue rules live in the adapters,
    which is why Shioaji refuses ROD market orders and GTC on its own."""

    @pytest.mark.parametrize("time_in_force", ["day", "gtc", "ioc", "fok"])
    def test_market_order_accepts_every_time_in_force(self, time_in_force: str) -> None:
        """A market order resolves on its first eligible event, so a lifetime
        is vacuous rather than unsupported — IBKR accepts DAY market orders
        that TAIFEX and Binance reject, and that is the adapter's call."""
        _validate(
            [OrderIntent(action="long", symbol="X", quantity=1.0, time_in_force=time_in_force)]
        )

    @pytest.mark.parametrize("time_in_force", ["day", "gtc"])
    def test_market_order_lifetime_collapses_to_immediate_execution(
        self, time_in_force: str
    ) -> None:
        result = _run(
            OrderIntent(action="long", symbol="X", quantity=5.0, time_in_force=time_in_force),
        )

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1
        assert opened[0].ts == _panel().index.get_level_values("datetime")[1]

    @pytest.mark.parametrize("time_in_force", ["day", "gtc"])
    def test_grouped_leg_cannot_rest(self, time_in_force: str) -> None:
        decision = [
            OrderIntent(
                action="long",
                symbol="X",
                quantity=1.0,
                limit_price=99.0,
                group_id="spread",
                time_in_force=time_in_force,
            ),
            OrderIntent(
                action="short",
                symbol="Y",
                quantity=1.0,
                limit_price=101.0,
                group_id="spread",
            ),
        ]

        with pytest.raises(ValueError, match="group"):
            _validate(decision)

    @pytest.mark.parametrize("time_in_force", ["ioc", "fok"])
    def test_grouped_leg_accepts_immediate_time_in_force(self, time_in_force: str) -> None:
        _validate(
            [
                OrderIntent(
                    action="long",
                    symbol="X",
                    quantity=1.0,
                    limit_price=99.0,
                    group_id="spread",
                    time_in_force=time_in_force,
                ),
                OrderIntent(
                    action="short",
                    symbol="Y",
                    quantity=1.0,
                    limit_price=101.0,
                    group_id="spread",
                    time_in_force=time_in_force,
                ),
            ]
        )

    @pytest.mark.parametrize("time_in_force", ["day", "gtc", "ioc", "fok"])
    def test_ungrouped_limit_order_accepts_every_time_in_force(self, time_in_force: str) -> None:
        _validate(
            [
                OrderIntent(
                    action="long",
                    symbol="X",
                    quantity=1.0,
                    limit_price=99.0,
                    time_in_force=time_in_force,
                )
            ]
        )

    def test_unset_time_in_force_stays_accepted_everywhere(self) -> None:
        _validate([OrderIntent(action="long", symbol="X", quantity=1.0)])


class TestImmediateTimeInForce:
    """IOC and FOK resolve on their first eligible event.

    "Enough liquidity" means librae's own participation cap, not book depth:
    the engine already commits to that abstraction for every fill, so a TIF
    that depends on available size is defined against it. Mainstream
    bar-based backtesters (backtrader, zipline, LEAN) model order lifetime
    but not IOC/FOK at all, because without a participation model there is
    nothing for "immediate" or "all" to be measured against.
    """

    def test_fok_books_nothing_when_the_participation_cap_shortens_the_fill(self) -> None:
        # Bar volume 100 at a 10% cap fills at most 10 units.
        result = _run(OrderIntent(action="long", symbol="X", quantity=20.0, time_in_force="fok"))

        assert result.position_events == []
        assert "fok_not_fully_fillable" in _skip_reasons(result)

    def test_fok_fills_entirely_when_it_fits_under_the_cap(self) -> None:
        result = _run(OrderIntent(action="long", symbol="X", quantity=5.0, time_in_force="fok"))

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1
        assert opened[0].fill_quantity == pytest.approx(5.0)
        assert "fok_not_fully_fillable" not in _skip_reasons(result)

    def test_ioc_keeps_the_capped_fill_and_cancels_the_remainder(self) -> None:
        result = _run(OrderIntent(action="long", symbol="X", quantity=20.0, time_in_force="ioc"))

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1
        assert opened[0].fill_quantity == pytest.approx(10.0)
        assert "ioc_remainder_cancelled" in _skip_reasons(result)

    def test_ioc_reports_the_cancelled_quantity(self) -> None:
        result = _run(OrderIntent(action="long", symbol="X", quantity=20.0, time_in_force="ioc"))

        cancelled = next(
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "ioc_remainder_cancelled"
        )
        assert cancelled.symbol == "X"
        assert float(cancelled.detail["cancelled_quantity"]) == pytest.approx(10.0)

    def test_ioc_that_fills_completely_cancels_nothing(self) -> None:
        result = _run(OrderIntent(action="long", symbol="X", quantity=5.0, time_in_force="ioc"))

        assert "ioc_remainder_cancelled" not in _skip_reasons(result)

    def test_unset_time_in_force_keeps_the_capped_partial_silently(self) -> None:
        """The historical default is unchanged: a short fill is booked and
        nothing is reported as cancelled, because no lifetime was requested."""
        result = _run(OrderIntent(action="long", symbol="X", quantity=20.0))

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert opened[0].fill_quantity == pytest.approx(10.0)
        assert _skip_reasons(result) == []

    def test_fok_entry_requires_an_explicit_quantity(self) -> None:
        """Cash-sized entries have no requested amount, so "all or none" has
        nothing to measure — the same reason groups require explicit sizes."""
        with pytest.raises(ValueError, match="explicit quantity"):
            _validate([OrderIntent(action="long", symbol="X", time_in_force="fok")])

    def test_fok_close_may_omit_quantity(self) -> None:
        """A close without a quantity means the whole position, which is a
        deterministic "all"."""
        _validate([OrderIntent(action="close", symbol="X", time_in_force="fok")])


def _reaches_limit_on_bar_three() -> pd.DataFrame:
    """Bar 3 is the first bar whose low reaches a 95.0 buy limit."""
    lows = [99.0, 99.0, 99.0, 94.0, 99.0, 99.0]
    index = pd.MultiIndex.from_product(
        [["X"], pd.date_range("2025-01-01", periods=len(lows), freq="h", tz="UTC")],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": [100.0] * len(lows),
            "high": [101.0] * len(lows),
            "low": lows,
            "close": [100.0] * len(lows),
            "volume": [1_000.0] * len(lows),
        },
        index=index,
    )


def _run_panel(panel: pd.DataFrame, intent: OrderIntent):
    return Backtest(
        panel,
        _submit_once(intent),
        initial_balance=100_000.0,
        cost_model=CostModel.zero(),
        data_source="test",
    ).run()


class TestRestingTimeInForce:
    """DAY and GTC keep a limit eligible on later events.

    Only price eligibility persists. An event that priced the order but
    refused it for an operational reason (cash, a participation cap, a
    notional limit) has resolved the decision and reports its own
    decision_skipped reason; retrying that silently every bar would hide a
    standing misconfiguration.
    """

    def test_gtc_limit_fills_on_a_later_bar(self) -> None:
        result = _run_panel(
            _reaches_limit_on_bar_three(),
            OrderIntent(
                action="long", symbol="X", quantity=1.0, limit_price=95.0, time_in_force="gtc"
            ),
        )

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1
        assert opened[0].price == pytest.approx(95.0)

    def test_unset_time_in_force_still_expires_after_one_event(self) -> None:
        """The historical one-shot default is unchanged."""
        result = _run_panel(
            _reaches_limit_on_bar_three(),
            OrderIntent(action="long", symbol="X", quantity=1.0, limit_price=95.0),
        )

        assert result.position_events == []

    def test_gtc_limit_that_never_reaches_books_nothing(self) -> None:
        panel = _reaches_limit_on_bar_three()
        result = _run_panel(
            panel,
            OrderIntent(
                action="long", symbol="X", quantity=1.0, limit_price=50.0, time_in_force="gtc"
            ),
        )

        assert result.position_events == []


def _hourly_btc(start: str, lows: list[float]) -> pd.DataFrame:
    """BTCUSDT owns calendar_id='24/7', so each UTC date is one session."""
    index = pd.MultiIndex.from_product(
        [["BTCUSDT"], pd.date_range(start, periods=len(lows), freq="h", tz="UTC")],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": [100.0] * len(lows),
            "high": [101.0] * len(lows),
            "low": lows,
            "close": [100.0] * len(lows),
            "volume": [1_000.0] * len(lows),
        },
        index=index,
    )


class TestDaySessionExpiry:
    """A "day" order lives until the session that submitted it ends."""

    def test_day_limit_rests_within_the_submitting_session(self) -> None:
        # 20:00..01:00 UTC: bar 3 (23:00) still belongs to the first session.
        panel = _hourly_btc("2025-01-01T20:00Z", [99.0, 99.0, 99.0, 94.0, 99.0, 99.0])

        result = _run_panel(
            panel,
            OrderIntent(
                action="long",
                symbol="BTCUSDT",
                quantity=1.0,
                limit_price=95.0,
                time_in_force="day",
            ),
        )

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1
        assert opened[0].price == pytest.approx(95.0)

    def test_day_limit_expires_once_the_session_rolls_over(self) -> None:
        # Submitted 22:00 on Jan 1; the limit is only reached at 01:00 on Jan 2,
        # which is a different 24/7 session.
        panel = _hourly_btc("2025-01-01T22:00Z", [99.0, 99.0, 99.0, 94.0, 99.0, 99.0])

        result = _run_panel(
            panel,
            OrderIntent(
                action="long",
                symbol="BTCUSDT",
                quantity=1.0,
                limit_price=95.0,
                time_in_force="day",
            ),
        )

        assert result.position_events == []
        assert "day_order_expired" in _skip_reasons(result)

    def test_gtc_survives_the_session_rollover_that_expires_day(self) -> None:
        panel = _hourly_btc("2025-01-01T22:00Z", [99.0, 99.0, 99.0, 94.0, 99.0, 99.0])

        result = _run_panel(
            panel,
            OrderIntent(
                action="long",
                symbol="BTCUSDT",
                quantity=1.0,
                limit_price=95.0,
                time_in_force="gtc",
            ),
        )

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1
        assert "day_order_expired" not in _skip_reasons(result)


class TestSimulationMatchesBacktest:
    """Simulation shares the executor with backtest, so order lifetime must
    not drift between them — the same reason stop/target timing is shared."""

    @staticmethod
    def _frame(bars: int) -> pd.DataFrame:
        lows = [99.0, 99.0, 99.0, 94.0, 99.0, 99.0][:bars]
        return pd.DataFrame(
            {
                "ts": pd.date_range("2025-01-01T00:00Z", periods=bars, freq="h", tz="UTC"),
                "open": [100.0] * bars,
                "high": [101.0] * bars,
                "low": lows,
                "close": [100.0] * bars,
                "volume": [1_000.0] * bars,
            }
        )

    def _run_sim(self, time_in_force: str | None):
        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        visible = {"bars": 2}

        def fetcher(*_args, **_kwargs):
            return self._frame(visible["bars"])

        class SubmitOnce(Strategy):
            def __init__(self) -> None:
                self.submitted = False

            def on_bar(self, ctx):
                if self.submitted:
                    return []
                self.submitted = True
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.0,
                        limit_price=95.0,
                        time_in_force=time_in_force,
                    )
                ]

        runner = TestLiveTrader()._make_runner(
            strategy=SubmitOnce(),
            fetcher=fetcher,
            config=_test_cfg(mode="sim", warmup_periods=1),
        )
        for bars in range(2, 7):
            visible["bars"] = bars
            runner._poll_cycle()
        return runner

    def test_gtc_limit_rests_until_a_later_bar_reaches_it(self) -> None:
        runner = self._run_sim("gtc")

        assert runner._positions, "a resting gtc limit should fill on the reaching bar"

    def test_unset_time_in_force_still_expires_after_one_event(self) -> None:
        runner = self._run_sim(None)

        assert not runner._positions
