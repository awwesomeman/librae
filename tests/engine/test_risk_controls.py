"""Tests for engine-level risk controls: max-position cap + max-drawdown circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    REASON_FORCE_CLOSE,
    _cap_fill_to_notional,
    _cap_fill_to_volume,
    calculate_position_weights,
    execute_order_intents,
    execute_pending_decision_and_stops,
    execute_portfolio_weights,
    liquidate_all,
)
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import (
    Context,
    Fill,
    OrderIntent,
    PortfolioWeights,
    PositionState,
    Strategy,
)

from tests.conftest import make_test_cfg

# ---------------------------------------------------------------------------
# Helpers — mirrors tests/engine/test_stop_targets.py / test_position_scaling.py
# ---------------------------------------------------------------------------


def _zero_cost() -> CostModel:
    return CostModel.zero()


def _leveraged_zero_cost() -> CostModel:
    return CostModel(
        multiplier=1.0,
        commission_rate=0.0,
        min_commission=0.0,
        slippage_ticks=0.0,
        tick_size=0.01,
        tax_rate=0.0,
        long_margin_rate=0.5,
        short_margin_rate=0.5,
    )


def _make_pos(
    symbol: str = "TEST",
    side: str = "long",
    entry_price: float = 100.0,
    quantity: float = 10.0,
) -> PositionState:
    return PositionState(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        entry_at=datetime(2026, 1, 1, tzinfo=UTC),
        periods_held=0,
        entry_commission=0.0,
        entry_slippage=0.0,
        entry_tax=0.0,
        total_entry_cost=entry_price * quantity,
    )


def _make_fill(price: float = 100.0, quantity: float = 10.0, side: str = "long") -> Fill:
    return Fill(
        symbol="TEST",
        side=side,
        price=price,
        quantity=quantity,
        commission=0.0,
        slippage=0.0,
        tax=0.0,
    )


def _make_multiindex_df(bars: list[dict[str, float]], symbol: str = "BTCUSDT") -> pd.DataFrame:
    n = len(bars)
    dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    idx = pd.MultiIndex.from_arrays([[symbol] * n, dt], names=["symbol", "datetime"])
    df = pd.DataFrame(bars, index=idx)
    if "volume" not in df.columns:
        df["volume"] = 100.0  # bars carrying their own "volume" key win instead
    return df


TS = datetime(2026, 1, 10, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Unit tests: _cap_fill_to_notional
# ---------------------------------------------------------------------------


class TestCapFillToNotional:
    def test_caps_a_fresh_fill(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_notional(
            fill, existing_qty=0.0, cost_model=_zero_cost(), max_notional=300.0
        )
        assert capped.quantity == pytest.approx(3.0)

    def test_caps_a_scale_in_against_existing_notional(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_notional(
            fill, existing_qty=2.0, cost_model=_zero_cost(), max_notional=500.0
        )
        # room = 500 - 2*100 = 300 -> 3 more units
        assert capped.quantity == pytest.approx(3.0)

    def test_no_room_left_returns_none(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_notional(
            fill, existing_qty=5.0, cost_model=_zero_cost(), max_notional=500.0
        )
        assert capped is None

    def test_under_cap_returns_fill_unchanged(self):
        fill = _make_fill(price=100.0, quantity=1.0)
        capped = _cap_fill_to_notional(
            fill, existing_qty=0.0, cost_model=_zero_cost(), max_notional=1000.0
        )
        assert capped is fill


# ---------------------------------------------------------------------------
# Unit tests: _cap_fill_to_volume
# ---------------------------------------------------------------------------


class TestCapFillToVolume:
    def test_caps_a_fresh_fill(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_volume(fill, cost_model=_zero_cost(), max_qty=4.0)
        assert capped.quantity == pytest.approx(4.0)

    def test_zero_max_qty_returns_none(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_volume(fill, cost_model=_zero_cost(), max_qty=0.0)
        assert capped is None

    def test_under_cap_returns_fill_unchanged(self):
        fill = _make_fill(price=100.0, quantity=1.0)
        capped = _cap_fill_to_volume(fill, cost_model=_zero_cost(), max_qty=10.0)
        assert capped is fill


# ---------------------------------------------------------------------------
# Unit tests: liquidate_all
# ---------------------------------------------------------------------------


class TestLiquidateAll:
    def test_closes_all_positions_and_sums_cash_delta(self):
        positions = {
            "A": _make_pos(symbol="A", side="long", entry_price=100.0, quantity=1.0),
            "B": _make_pos(symbol="B", side="short", entry_price=100.0, quantity=1.0),
        }
        bars = {"A": {"close": 110.0}, "B": {"close": 90.0}}
        result = liquidate_all(
            positions,
            bars,
            TS,
            get_cost_model=lambda s: _zero_cost(),
            reason=REASON_DRAWDOWN_BREACH,
        )
        assert positions == {}
        assert len(result.trades) == 2
        assert all(e.reason == REASON_DRAWDOWN_BREACH for e in result.events)
        # long +10 gain, short +10 gain (price moved against short's loss side... both favorable here)
        assert result.cash_delta == pytest.approx(220.0)

    def test_missing_bar_does_not_invent_a_liquidation_price(self):
        positions = {"A": _make_pos(symbol="A", entry_price=100.0, quantity=1.0)}
        result = liquidate_all(
            positions,
            {},
            TS,
            get_cost_model=lambda s: _zero_cost(),
            reason=REASON_FORCE_CLOSE,
        )
        assert set(positions) == {"A"}
        assert result.trades == []
        assert result.events == []

    def test_volume_constrained_liquidation_is_partial(self):
        positions = {"A": _make_pos(symbol="A", entry_price=100.0, quantity=10.0)}
        result = liquidate_all(
            positions,
            {"A": {"close": 110.0, "volume": 20.0}},
            TS,
            get_cost_model=lambda s: _zero_cost(),
            reason=REASON_FORCE_CLOSE,
            max_bar_volume_participation_rate=0.25,
        )

        assert result.events[0].event_type == "reduce"
        assert result.events[0].fill_quantity == pytest.approx(5.0)
        assert positions["A"].quantity == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Unit tests: execute_order_intents respects max_position_notional
# ---------------------------------------------------------------------------


class TestProcessActionsPositionCap:
    def test_oversized_open_gets_clamped(self):
        actions = [OrderIntent(action="long", symbol="TEST")]
        result = execute_order_intents(
            actions,
            {},
            10_000.0,
            TS,
            get_price=lambda s, a: 100.0,
            get_cost_model=lambda s: _zero_cost(),
            primary_symbol="TEST",
            max_position_notional=300.0,
        )
        assert len(result.events) == 1
        assert result.events[0].fill_quantity == pytest.approx(3.0)


class TestMaxOrderNotional:
    def test_oversized_entry_is_rejected_before_mutation(self):
        positions: dict[str, PositionState] = {}

        with pytest.raises(ValueError, match="max_order_notional"):
            execute_order_intents(
                [OrderIntent(action="long", symbol="TEST", quantity=4.0)],
                positions,
                10_000.0,
                TS,
                get_price=lambda s, a: 100.0,
                get_cost_model=lambda s: _zero_cost(),
                primary_symbol="TEST",
                max_order_notional=300.0,
            )

        assert positions == {}

    def test_risk_reducing_close_is_not_blocked(self):
        positions = {"TEST": _make_pos(quantity=10.0)}

        result = execute_order_intents(
            [OrderIntent(action="close", symbol="TEST")],
            positions,
            0.0,
            TS,
            get_price=lambda s, a: 100.0,
            get_cost_model=lambda s: _zero_cost(),
            primary_symbol="TEST",
            max_order_notional=300.0,
        )

        assert len(result.events) == 1
        assert positions == {}

    def test_portfolio_target_addition_uses_same_limit(self):
        positions: dict[str, PositionState] = {}

        with pytest.raises(ValueError, match="max_order_notional"):
            execute_portfolio_weights(
                PortfolioWeights(weights={"TEST": 0.5}),
                positions,
                1_000.0,
                TS,
                get_price=lambda s, a: 100.0,
                get_cost_model=lambda s: _zero_cost(),
                primary_symbol="TEST",
                max_order_notional=300.0,
            )

        assert positions == {}


class TestPortfolioExposureLimits:
    def test_canonical_position_weights_preserve_direction(self):
        positions = {
            "AAA": _make_pos(symbol="AAA", side="long", quantity=2.0),
            "BBB": _make_pos(symbol="BBB", side="short", quantity=1.0),
        }

        weights = calculate_position_weights(
            positions,
            1_000.0,
            prices={"AAA": 100.0, "BBB": 100.0},
            get_cost_model=lambda _symbol: _zero_cost(),
        )

        assert weights == pytest.approx({"AAA": 0.2, "BBB": -0.1})

    def test_order_intent_batch_cannot_bypass_gross_limit(self):
        positions: dict[str, PositionState] = {}
        bars = {
            "AAA": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            "BBB": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        }

        with pytest.raises(ValueError, match="post-decision gross exposure"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                200.0,
                [
                    OrderIntent(action="long", symbol="AAA", quantity=1.5),
                    OrderIntent(action="long", symbol="BBB", quantity=1.5),
                ],
                bars,
                get_cost_model=lambda _symbol: _leveraged_zero_cost(),
                default_fill="open",
                primary_symbol="AAA",
                max_gross_exposure=1.0,
                exposure_prices={"AAA": 100.0, "BBB": 100.0},
            )

        assert positions == {}

    def test_rejected_exposure_batch_does_not_consume_adv_budget(self):
        positions: dict[str, PositionState] = {}
        adv_usage: dict[str, float] = {}
        bars = {
            "AAA": {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 100.0,
            },
        }

        with pytest.raises(ValueError, match="post-decision gross exposure"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                200.0,
                [OrderIntent(action="long", symbol="AAA", quantity=1.5)],
                bars,
                get_cost_model=lambda _symbol: _leveraged_zero_cost(),
                default_fill="open",
                primary_symbol="AAA",
                max_adv_participation_rate=1.0,
                get_lagged_adv=lambda _symbol: 100.0,
                used_adv_quantity_by_symbol=adv_usage,
                max_gross_exposure=0.5,
                exposure_prices={"AAA": 100.0},
            )

        assert positions == {}
        assert adv_usage == {}

    def test_order_intent_batch_cannot_bypass_net_limit(self):
        positions: dict[str, PositionState] = {}
        bars = {
            "AAA": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        }

        with pytest.raises(ValueError, match="post-decision absolute net exposure"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                200.0,
                [OrderIntent(action="long", symbol="AAA", quantity=1.5)],
                bars,
                get_cost_model=lambda _symbol: _leveraged_zero_cost(),
                default_fill="open",
                primary_symbol="AAA",
                max_net_exposure=0.5,
                exposure_prices={"AAA": 100.0},
            )

        assert positions == {}

    def test_grouped_batch_cannot_bypass_gross_limit(self):
        positions: dict[str, PositionState] = {}
        bars = {
            "AAA": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            "BBB": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        }
        decision = [
            OrderIntent(action="long", symbol="AAA", quantity=1.5, group_id="g"),
            OrderIntent(action="short", symbol="BBB", quantity=1.5, group_id="g"),
        ]

        with pytest.raises(ValueError, match="post-decision gross exposure"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                200.0,
                decision,
                bars,
                get_cost_model=lambda _symbol: _leveraged_zero_cost(),
                default_fill="open",
                primary_symbol="AAA",
                max_gross_exposure=1.0,
                exposure_prices={"AAA": 100.0, "BBB": 100.0},
            )

        assert positions == {}

    def test_risk_reduction_remains_allowed_while_still_above_limit(self):
        positions = {"TEST": _make_pos(quantity=2.0)}
        bars = {
            "TEST": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        }

        _, result = execute_pending_decision_and_stops(
            TS,
            positions,
            0.0,
            [OrderIntent(action="close", symbol="TEST", quantity=0.5)],
            bars,
            get_cost_model=lambda _symbol: _zero_cost(),
            default_fill="open",
            primary_symbol="TEST",
            max_gross_exposure=0.5,
            exposure_prices={"TEST": 100.0},
        )

        assert len(result.events) == 1
        assert positions["TEST"].quantity == pytest.approx(1.5)

    def test_portfolio_target_can_reduce_risk_while_still_above_limit(self):
        positions = {"TEST": _make_pos(quantity=2.0)}
        bars = {
            "TEST": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        }

        _, result = execute_pending_decision_and_stops(
            TS,
            positions,
            0.0,
            PortfolioWeights(weights={"TEST": 0.75}),
            bars,
            get_cost_model=lambda _symbol: _zero_cost(),
            default_fill="open",
            primary_symbol="TEST",
            max_gross_exposure=0.5,
            exposure_prices={"TEST": 100.0},
        )

        assert len(result.events) == 1
        assert positions["TEST"].quantity == pytest.approx(1.5)

    def test_risk_reducing_close_remains_available_with_non_positive_equity(self):
        positions = {"TEST": _make_pos(quantity=2.0)}
        bars = {
            "TEST": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        }

        cash, result = execute_pending_decision_and_stops(
            TS,
            positions,
            -250.0,
            [OrderIntent(action="close", symbol="TEST", quantity=0.5)],
            bars,
            get_cost_model=lambda _symbol: _zero_cost(),
            default_fill="open",
            primary_symbol="TEST",
            max_gross_exposure=0.5,
            exposure_prices={"TEST": 100.0},
        )

        assert len(result.events) == 1
        assert positions["TEST"].quantity == pytest.approx(1.5)
        assert cash == pytest.approx(-200.0)


class TestProcessActionsVolumeCap:
    def test_oversized_open_gets_clamped_to_participation(self):
        actions = [OrderIntent(action="long", symbol="TEST")]
        result = execute_order_intents(
            actions,
            {},
            10_000.0,
            TS,
            get_price=lambda s, a: 100.0,
            get_cost_model=lambda s: _zero_cost(),
            primary_symbol="TEST",
            max_bar_volume_participation_rate=0.1,
            get_volume=lambda s: 50.0,
        )
        assert len(result.events) == 1
        assert result.events[0].fill_quantity == pytest.approx(5.0)  # 0.1 * 50

    def test_zero_bar_volume_rejects_rather_than_skips_cap(self):
        """Regression test: a real zero-volume bar must reject the fill
        outright (0% of 0 volume = 0), not be treated as "no volume data
        available" and let an uncapped fill through — the exact opposite
        of what a volume-participation cap is supposed to guarantee."""
        actions = [OrderIntent(action="long", symbol="TEST")]
        result = execute_order_intents(
            actions,
            {},
            10_000.0,
            TS,
            get_price=lambda s, a: 100.0,
            get_cost_model=lambda s: _zero_cost(),
            primary_symbol="TEST",
            max_bar_volume_participation_rate=0.1,
            get_volume=lambda s: 0.0,
        )
        assert result.events == []

    def test_missing_volume_data_rejects_when_cap_is_enabled(self):
        """No volume data (get_volume returns None) is a different case
        from a real zero-volume bar — the cap can't be computed, so it's
        skipped rather than treated as a hard reject."""
        actions = [OrderIntent(action="long", symbol="TEST")]
        result = execute_order_intents(
            actions,
            {},
            10_000.0,
            TS,
            get_price=lambda s, a: 100.0,
            get_cost_model=lambda s: _zero_cost(),
            primary_symbol="TEST",
            max_bar_volume_participation_rate=0.1,
            get_volume=lambda s: None,
        )
        assert result.events == []

    def test_reduction_is_capped_and_uses_exit_volume_for_impact(self):
        positions = {"TEST": _make_pos(quantity=10.0)}
        cost_model = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=1.0,
            tick_size=0.01,
            tax_rate=0.0,
            volume_impact_ticks=10.0,
        )

        result = execute_order_intents(
            [OrderIntent(action="close", symbol="TEST")],
            positions,
            0.0,
            TS,
            get_price=lambda s, a: 110.0,
            get_cost_model=lambda s: cost_model,
            primary_symbol="TEST",
            max_bar_volume_participation_rate=0.25,
            get_volume=lambda s: 20.0,
        )

        assert result.events[0].event_type == "reduce"
        assert result.events[0].fill_quantity == pytest.approx(5.0)
        assert result.events[0].slippage == pytest.approx(0.175)
        assert positions["TEST"].quantity == pytest.approx(5.0)

    def test_multiple_fills_share_one_symbol_volume_budget(self):
        result = execute_order_intents(
            [
                OrderIntent(action="long", symbol="TEST", quantity=3.0),
                OrderIntent(action="long", symbol="TEST", quantity=3.0),
            ],
            {},
            10_000.0,
            TS,
            get_price=lambda s, a: 100.0,
            get_cost_model=lambda s: _zero_cost(),
            primary_symbol="TEST",
            max_bar_volume_participation_rate=0.25,
            get_volume=lambda s: 20.0,
        )

        assert [event.fill_quantity for event in result.events] == pytest.approx([3.0, 2.0])
        assert sum(event.fill_quantity for event in result.events) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Integration tests: full Backtest run
# ---------------------------------------------------------------------------


class OpenOnceStrategy(Strategy):
    """Opens a long at bar 1, never closes itself — isolates engine-enforced exits."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.period_index == 1 and ctx.symbol not in ctx.positions:
            return [OrderIntent(action="long", symbol=ctx.symbol)]
        return []


class TestMaxDrawdownBreaker:
    def test_breach_flattens_and_halts(self):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 60, "high": 65, "low": 55, "close": 60},  # crater -> breach
            {"open": 60, "high": 61, "low": 59, "close": 60},  # halted: stays flat
        ]
        cfg = make_test_cfg(
            mode="backtest",
            initial_balance=10_000.0,
            risk=RiskPolicy(max_drawdown_rate=0.2),
        )
        bt = Backtest(
            _make_multiindex_df(bars), OpenOnceStrategy(), config=cfg, cost_model=_zero_cost()
        )
        result = bt.run()

        close_events = [e for e in result.order_events if e.event_type == "close"]
        assert len(close_events) == 1
        assert close_events[0].reason == REASON_DRAWDOWN_BREACH
        assert len(result.trades) == 1  # no further trades after the breach

        assert len(result.equity_curve) == len(bars)  # curve continues to the end, flat
        assert result.equity_curve[-1].equity == pytest.approx(result.equity_curve[3].equity)

    def test_breach_queues_exit_for_next_bar_open(self) -> None:
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 60, "high": 65, "low": 55, "close": 60},
            {"open": 60, "high": 61, "low": 59, "close": 60},
        ]
        cfg = make_test_cfg(
            mode="backtest",
            initial_balance=10_000.0,
            risk=RiskPolicy(max_drawdown_rate=0.2),
        )
        cost = CostModel(
            multiplier=1.0,
            commission_rate=0.01,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        bt = Backtest(
            _make_multiindex_df(bars),
            OpenOnceStrategy(),
            config=cfg,
            cost_model=cost,
            record_position_snapshots=True,
        )

        result = bt.run()

        close_event = next(event for event in result.order_events if event.event_type == "close")
        breach_ts = result.equity_curve[3].ts
        exit_ts = result.equity_curve[4].ts
        assert close_event.reason == REASON_DRAWDOWN_BREACH
        assert close_event.ts == exit_ts
        assert close_event.price == pytest.approx(bars[4]["open"])
        assert close_event.commission > 0
        assert result.equity_curve[4].equity == pytest.approx(result.final_equity)
        assert any(snapshot.ts == breach_ts for snapshot in result.position_snapshots)
        assert all(snapshot.ts != exit_ts for snapshot in result.position_snapshots)

    def test_no_breach_when_disabled(self):
        """Same crash, no max_drawdown_rate set -> position rides it out (baseline sanity check)."""
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 60, "high": 65, "low": 55, "close": 60},
            {"open": 60, "high": 61, "low": 59, "close": 60},
        ]
        bt = Backtest(_make_multiindex_df(bars), OpenOnceStrategy(), cost_model=_zero_cost())
        result = bt.run()

        close_events = [e for e in result.order_events if e.event_type == "close"]
        assert len(close_events) == 1
        assert close_events[0].reason == REASON_FORCE_CLOSE  # only closed by end-of-run liquidation

    def test_final_bar_breach_fails_instead_of_same_bar_fill(self) -> None:
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 10, "high": 50, "low": 9, "close": 50},
        ]
        cfg = make_test_cfg(
            mode="backtest",
            initial_balance=10_000.0,
            risk=RiskPolicy(max_drawdown_rate=0.2),
        )

        with pytest.raises(ValueError, match="without a subsequent tradable bar"):
            Backtest(
                _make_multiindex_df(bars),
                OpenOnceStrategy(),
                config=cfg,
                cost_model=_zero_cost(),
            ).run()


class TestMaxPositionCap:
    def test_open_clamped_to_pct_of_equity(self):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
        ]
        cfg = make_test_cfg(
            mode="backtest",
            initial_balance=10_000.0,
            risk=RiskPolicy(max_position_weight=0.3),
        )
        bt = Backtest(
            _make_multiindex_df(bars), OpenOnceStrategy(), config=cfg, cost_model=_zero_cost()
        )
        result = bt.run()

        assert len(result.trades) == 1
        # Uncapped, all ~10_000 cash at price 100 would size ~100 units.
        # max_position_weight=0.3 against last-known equity (10_000) caps notional at 3_000 -> 30 units.
        assert result.trades[0].quantity == pytest.approx(30.0)


class TestMaxVolumeParticipation:
    def test_open_clamped_to_pct_of_bar_volume(self):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
            {
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 20.0,
            },  # fill happens here
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
        ]
        cfg = make_test_cfg(
            mode="backtest",
            initial_balance=10_000.0,
            execution=ExecutionPolicy(max_bar_volume_participation_rate=0.5),
        )
        bt = Backtest(
            _make_multiindex_df(bars), OpenOnceStrategy(), config=cfg, cost_model=_zero_cost()
        )
        result = bt.run()

        assert len(result.trades) == 1
        # Uncapped, all ~10_000 cash at price 100 would size ~100 units.
        # max_bar_volume_participation_rate=0.5 * bar[2].volume(20) caps it at 10 units.
        assert result.trades[0].quantity == pytest.approx(10.0)


class TestDynamicSlippage:
    def test_all_cash_sizing_includes_volume_impact(self) -> None:
        cost_model = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=1.0,
            tax_rate=0.0,
            volume_impact_ticks=10.0,
        )
        cash = 1_000.0

        result = execute_order_intents(
            [OrderIntent(action="long", symbol="TEST")],
            {},
            cash,
            datetime(2025, 1, 1, tzinfo=UTC),
            get_price=lambda _symbol, _intent: 100.0,
            get_cost_model=lambda _symbol: cost_model,
            primary_symbol="TEST",
            get_volume=lambda _symbol: 10.0,
        )

        assert len(result.events) == 1
        assert result.events[0].fill_quantity < 10.0
        assert cash + result.cash_delta >= -1e-9

    def test_lower_bar_volume_produces_higher_slippage(self):
        """Same fixed-size entry, only the fill bar's volume differs -> the
        low-volume run's participation-scaled slippage must be strictly
        higher, proving volume_impact_ticks actually moves the number end-to-end."""

        class OpenFixedQtyStrategy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index == 1 and ctx.symbol not in ctx.positions:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=5.0)]
                return []

        cost_model = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=1.0,
            tick_size=0.01,
            tax_rate=0.0,
            volume_impact_ticks=10.0,
        )

        def _bars(fill_bar_volume: float) -> list[dict[str, float]]:
            return [
                {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
                {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
                {"open": 100, "high": 101, "low": 99, "close": 100, "volume": fill_bar_volume},
                {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
                {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0},
            ]

        def _open_slippage(fill_bar_volume: float) -> float:
            bt = Backtest(
                _make_multiindex_df(_bars(fill_bar_volume)),
                OpenFixedQtyStrategy(),
                cost_model=cost_model,
                execution=ExecutionPolicy(max_bar_volume_participation_rate=None),
            )
            result = bt.run()
            open_events = [e for e in result.order_events if e.event_type == "open"]
            assert len(open_events) == 1
            return open_events[0].slippage

        high_vol_slippage = _open_slippage(1000.0)  # 0.5% participation
        low_vol_slippage = _open_slippage(10.0)  # 50% participation

        assert low_vol_slippage > high_vol_slippage
        # participation=0.005 -> +0.05 impact ticks -> 1.05 ticks -> 1.05*0.01*5 = 0.0525
        assert high_vol_slippage == pytest.approx(0.0525)
        # participation=0.5 -> +5 impact ticks -> 6 ticks -> 6*0.01*5 = 0.30
        assert low_vol_slippage == pytest.approx(0.30)
