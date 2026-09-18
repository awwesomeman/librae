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
    """Every decision_skipped reason, including ones coalesce_runtime_events
    demoted into related_events when another reason shared the same ts and
    symbol — the audit trail keeps them, so assertions must see them too."""
    reasons: list[str] = []
    for event in result.runtime_events:
        if event.event_type != "decision_skipped":
            continue
        reasons.append(str(event.detail.get("reason")))
        for related in event.detail.get("related_events", []):
            if isinstance(related, dict) and "reason" in related:
                reasons.append(str(related["reason"]))
    return reasons


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


def _panel_for(symbol: str, lows: list[float], *, freq: str = "h", volumes=None) -> pd.DataFrame:
    n = len(lows)
    index = pd.MultiIndex.from_product(
        [[symbol], pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": lows,
            "close": [100.0] * n,
            "volume": list(volumes) if volumes else [1_000.0] * n,
        },
        index=index,
    )


class TestRestingLifetimeStartsAtTheFirstEligibleEvent:
    """A decision is emitted on one bar and first executable on the next, so a
    lifetime anchored to the emitting bar would expire before the order was
    ever eligible — on daily data it could never fill at all."""

    def test_day_limit_fills_on_its_first_eligible_daily_bar(self) -> None:
        panel = _panel_for("X", [99.0, 94.0, 99.0, 99.0, 99.0, 99.0], freq="D")

        result = _run_panel(
            panel,
            OrderIntent(
                action="long", symbol="X", quantity=1.0, limit_price=95.0, time_in_force="day"
            ),
        )

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1, "a day limit must be eligible on the bar that executes it"
        assert "day_order_expired" not in _skip_reasons(result)

    def test_day_limit_emitted_on_the_last_bar_of_a_session_survives_into_the_next(self) -> None:
        """A broker treats an order submitted after the close as a day order
        for the following session rather than expiring it unfilled."""
        panel = _hourly_btc("2025-01-01T23:00Z", [99.0, 94.0, 99.0, 99.0, 99.0, 99.0])

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


class TestReEmissionReplacesARestingOrder:
    """ "gtc" commits an order until it fills or the run ends, so a strategy
    needs a way out. A new decision for the symbol is that way out."""

    def test_a_new_intent_replaces_the_resting_one(self) -> None:
        panel = _panel_for("X", [99.0] * 6)

        class ReplaceOnBarTwo(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [
                        OrderIntent(
                            action="long",
                            symbol="X",
                            quantity=1.0,
                            limit_price=95.0,
                            time_in_force="gtc",
                        )
                    ]
                if ctx.period_index == 2:
                    return [OrderIntent(action="long", symbol="X", quantity=1.0)]
                return []

        result = Backtest(
            panel,
            ReplaceOnBarTwo(),
            initial_balance=100_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
        ).run()

        opened = [event for event in result.position_events if event.event_type == "open"]
        assert len(opened) == 1, "the replacing market order should fill"
        assert "resting_order_replaced" in _skip_reasons(result)


class TestCloseHonoursTimeInForce:
    """The close branch never reached _try_fill, so a fok close could book a
    partial reduce — the exact outcome fok exists to prevent."""

    @staticmethod
    def _close_with(tif: str | None):
        volumes = [1_000.0, 1_000.0, 100.0, 1_000.0, 1_000.0, 1_000.0]
        panel = _panel_for("X", [99.0] * 6, volumes=volumes)

        class OpenThenClose(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol="X", quantity=10.0)]
                if ctx.period_index == 2:
                    return [
                        OrderIntent(action="close", symbol="X", quantity=10.0, time_in_force=tif)
                    ]
                return []

        return Backtest(
            panel,
            OpenThenClose(),
            initial_balance=100_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(max_bar_volume_participation_rate=0.05),
        ).run()

    def test_fok_close_cancels_instead_of_booking_a_partial_reduce(self) -> None:
        result = self._close_with("fok")

        # The run's own end-of-data liquidation is not the decision under test.
        strategy_driven = [
            event.event_type for event in result.position_events if event.reason != "force_close"
        ]
        assert strategy_driven == ["open"]
        assert "fok_not_fully_fillable" in _skip_reasons(result)

    def test_ioc_close_keeps_the_partial_and_reports_the_remainder(self) -> None:
        result = self._close_with("ioc")

        reduced = [event for event in result.position_events if event.event_type == "reduce"]
        assert len(reduced) == 1
        assert reduced[0].fill_quantity == pytest.approx(5.0)
        assert "ioc_remainder_cancelled" in _skip_reasons(result)

    def test_unset_close_keeps_the_partial_silently(self) -> None:
        result = self._close_with(None)

        reduced = [event for event in result.position_events if event.event_type == "reduce"]
        assert len(reduced) == 1
        assert _skip_reasons(result) == []


def test_day_expiry_skips_symbols_without_a_bar_on_this_event() -> None:
    """On a union timeline another instrument's bar can fall outside this
    symbol's session entirely. That is not a boundary its order crossed, and
    asking for a session label there raises."""
    from librae.backtest.engine import _DecisionEnvelope, _expire_day_intents

    resting = OrderIntent(
        action="long", symbol="TW", quantity=1.0, limit_price=95.0, time_in_force="day"
    )
    ts = pd.Timestamp("2025-01-01T02:00Z")
    envelope = _DecisionEnvelope([resting], None, pd.Timestamp("2025-01-01T01:00Z"))

    def exploding_session_of(symbol: str, when: pd.Timestamp) -> object:
        raise ValueError(f"{when} is outside the XTAI trading session")

    kept, expired = _expire_day_intents(
        [envelope],
        ts,
        {"BTCUSDT"},
        primary_symbol="BTCUSDT",
        session_of=exploding_session_of,
    )

    assert expired == []
    assert kept[0].decision == [resting]


def test_intraday_day_limit_without_a_calendar_fails_on_the_emitting_bar() -> None:
    """Engine expressibility, not a venue rule — so it surfaces with the
    decision that asked for it rather than one bar later."""
    panel = _panel_for("X", [99.0] * 6)

    with pytest.raises(ValueError, match="calendar_id"):
        _run_panel(
            panel,
            OrderIntent(
                action="long", symbol="X", quantity=1.0, limit_price=95.0, time_in_force="day"
            ),
        )


class TestSimulationMirrorsTheFixedBacktest:
    def _run_sim(self, strategy):
        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        visible = {"bars": 2}
        # The decision is emitted on the newest visible bar and first executable
        # on the next one, so the reaching bar must come after the submission.
        lows = [99.0, 99.0, 99.0, 99.0, 94.0, 99.0]

        def fetcher(*_args, **_kwargs):
            n = visible["bars"]
            return pd.DataFrame(
                {
                    "ts": pd.date_range("2025-01-01T00:00Z", periods=n, freq="h", tz="UTC"),
                    "open": [100.0] * n,
                    "high": [101.0] * n,
                    "low": lows[:n],
                    "close": [100.0] * n,
                    "volume": [1_000.0] * n,
                }
            )

        runner = TestLiveTrader()._make_runner(
            strategy=strategy,
            fetcher=fetcher,
            config=_test_cfg(mode="sim", warmup_periods=1),
        )
        events: list = []
        runner._on_runtime_event = events.append
        for bars in range(2, 7):
            visible["bars"] = bars
            runner._poll_cycle()
        return runner, events

    def test_day_limit_is_eligible_on_the_event_that_executes_it(self) -> None:
        class SubmitOnce(Strategy):
            def __init__(self) -> None:
                self.done = False

            def on_bar(self, ctx):
                if self.done:
                    return []
                self.done = True
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.0,
                        limit_price=95.0,
                        time_in_force="day",
                    )
                ]

        runner, _ = self._run_sim(SubmitOnce())

        assert runner._positions

    def test_a_new_intent_replaces_a_resting_one(self) -> None:
        class ReplaceLater(Strategy):
            def __init__(self) -> None:
                self.calls = 0

            def on_bar(self, ctx):
                self.calls += 1
                if self.calls == 1:
                    return [
                        OrderIntent(
                            action="long",
                            symbol=ctx.symbol,
                            quantity=1.0,
                            limit_price=1.0,
                            time_in_force="gtc",
                        )
                    ]
                if self.calls == 3:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
                return []

        runner, events = self._run_sim(ReplaceLater())

        assert runner._positions, "the replacing market order should fill"
        assert any(e.detail.get("reason") == "resting_order_replaced" for e in events)


def _two_calendar_panel() -> pd.DataFrame:
    """A union timeline where one symbol trades 24/7 and the other only
    inside the XNYS session, so most bars belong to one instrument alone."""
    rows: list[tuple[str, pd.Timestamp]] = []
    for ts in pd.date_range("2025-01-02T00:00Z", periods=48, freq="h", tz="UTC"):
        rows.append(("CRYPTO24", ts))
        if 15 <= ts.hour <= 20:
            rows.append(("EQUITY", ts))
    index = pd.MultiIndex.from_tuples(rows, names=["symbol", "datetime"]).sort_values()
    size = len(index)
    return pd.DataFrame(
        {
            "open": [100.0] * size,
            "high": [101.0] * size,
            "low": [99.0] * size,
            "close": [100.0] * size,
            "volume": [1_000.0] * size,
        },
        index=index,
    )


def _two_calendar_config():
    from librae.core.run_config import AccountConfig, RunConfig

    shared = {"data_adapter": "test", "instrument_type": "spot", "currency": "USD"}
    return RunConfig(
        strategy_name="two-calendar",
        mode="backtest",
        symbols=["CRYPTO24", "EQUITY"],
        timeframe="H1",
        market="us_equity",
        data_source="test",
        account=AccountConfig(currency="USD", initial_cash=100_000.0),
        symbol_cost_overrides={"EQUITY": {"multiplier": 1.0}, "CRYPTO24": {"multiplier": 1.0}},
        instrument_overrides={
            "EQUITY": {**shared, "calendar_id": "XNYS"},
            "CRYPTO24": {**shared, "calendar_id": "24/7"},
        },
    )


class TestUnionTimelineDoesNotBorrowAnotherCalendar:
    """Most bars on a union timeline belong to one instrument alone. Asking
    for another symbol's session label there raises, and that timestamp is
    not a boundary its orders ever crossed."""

    def test_day_limit_may_be_emitted_during_another_instruments_session(self) -> None:
        class EmitOffSession(Strategy):
            def on_bar(self, ctx):
                # 02:00Z: only CRYPTO24 has a bar, and it lies outside every
                # XNYS session.
                if ctx.ts.hour == 2 and ctx.ts.day == 2:
                    return [
                        OrderIntent(
                            action="long",
                            symbol="EQUITY",
                            quantity=1.0,
                            limit_price=95.0,
                            time_in_force="day",
                        )
                    ]
                return []

        result = Backtest(
            _two_calendar_panel(),
            EmitOffSession(),
            config=_two_calendar_config(),
            cost_model=CostModel.zero(),
        ).run()

        assert result.position_events == []

    def test_resting_day_limit_survives_another_instruments_bars(self) -> None:
        """The order rests from an XNYS bar and must not be expired — or
        crash — by the crypto bars that follow it overnight."""

        class RestOverAnotherCalendar(Strategy):
            def on_bar(self, ctx):
                if ctx.ts.hour == 15 and ctx.ts.day == 2:
                    return [
                        OrderIntent(
                            action="long",
                            symbol="EQUITY",
                            quantity=1.0,
                            limit_price=95.0,
                            time_in_force="day",
                        )
                    ]
                return []

        result = Backtest(
            _two_calendar_panel(),
            RestOverAnotherCalendar(),
            config=_two_calendar_config(),
            cost_model=CostModel.zero(),
        ).run()

        expiries = [reason for reason in _skip_reasons(result) if reason == "day_order_expired"]
        assert len(expiries) <= 1, "expiry must be judged once, on the symbol's own bars"
