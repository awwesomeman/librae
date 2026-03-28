"""Backtest engine — bar-by-bar execution with Strategy + Executor pattern.

Usage:
    bt = Backtest(data=df, strategy=my_strategy, market="crypto")
    result = bt.run()

    # With builder
    result = (Backtest(data=df, strategy=my_strategy, market="crypto")
              .set_benchmark("auto")
              .run())

The engine owns all position state. Strategies only observe via Context.
Execution is delegated to an Executor (BacktestExecutor for simulation).

Data format: MultiIndex DataFrame (instrument, datetime) with OHLCV + features.
Single-asset is a special case where instruments has one element.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np
import pandas as pd

from .cost_model import CostModel
from .executor import BacktestExecutor
from .strategy import Action, BaseStrategy, Context, Fill, Position

logger = logging.getLogger(__name__)

EPSILON = 1e-9


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeResult:
    """Single completed trade from the engine."""

    instrument: str
    entry_ts: datetime
    exit_ts: datetime
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    commission: float
    slippage: float
    tax: float
    net_pnl: float
    gross_return: float
    net_return: float
    holding_bars: int


@dataclass(frozen=True)
class EquitySnapshot:
    """Single bar equity snapshot."""

    ts: datetime
    equity: float


@dataclass(frozen=True)
class BacktestResult:
    """Raw output from engine — no metrics, just facts."""

    trades: Sequence[TradeResult]
    equity_curve: Sequence[EquitySnapshot]
    initial_balance: float
    final_equity: float
    benchmark_curve: Sequence[float] | None = None


# ---------------------------------------------------------------------------
# Internal mutable position state
# ---------------------------------------------------------------------------


@dataclass
class _PositionState:
    instrument: str
    side: str
    entry_price: float
    quantity: float
    entry_ts: datetime
    bars_held: int

    @property
    def direction(self) -> float:
        return -1.0 if self.side == "short" else 1.0
    entry_commission: float
    entry_slippage: float


# ---------------------------------------------------------------------------
# Backtest class
# ---------------------------------------------------------------------------


class Backtest:
    """Bar-by-bar backtest engine.

    Args:
        data: MultiIndex DataFrame (instrument, datetime) with OHLCV + features.
              For single-asset, wrap with: pd.MultiIndex.from_arrays([["SYM"]*len(df), df.index])
        strategy: BaseStrategy subclass.
        market: Market key from markets.yaml (e.g. "crypto", "tw_futures").
        initial_balance: Starting cash.
        executor: Custom Executor (overrides market config).
    """

    def __init__(
        self,
        data: pd.DataFrame,
        strategy: BaseStrategy,
        market: str | None = None,
        initial_balance: float = 100_000.0,
        executor: BacktestExecutor | None = None,
    ) -> None:
        self._data = data
        self._strategy = strategy
        self._initial_balance = initial_balance
        self._benchmark: str | pd.Series | None = "auto"

        # Parse instruments from MultiIndex
        if not isinstance(data.index, pd.MultiIndex):
            raise ValueError(
                "data must have MultiIndex (instrument, datetime). "
                "For single-asset: df.index = pd.MultiIndex.from_arrays([['SYM']*len(df), df.index])"
            )

        self._instruments = data.index.get_level_values(0).unique().tolist()
        self._time_groups = data.groupby(level="datetime")
        self._timeline = sorted(self._time_groups.groups.keys())

        # Build executors: executor > market > zero-cost fallback
        if executor is not None:
            self._executors = {inst: executor for inst in self._instruments}
        elif market is not None:
            self._executors = self._build_from_market(market)
        else:
            default_exec = BacktestExecutor(CostModel.zero())
            self._executors = {inst: default_exec for inst in self._instruments}

    # --- Builder methods ---

    def set_benchmark(self, val: str | pd.Series | None) -> Backtest:
        """Set benchmark mode. Returns self for chaining.

        Args:
            val: "auto" (buy-and-hold for single asset), pd.Series (custom), or None (skip).
        """
        self._benchmark = val
        return self

    # --- Private helpers ---

    @staticmethod
    def _build_from_market(market_name: str) -> dict[str, BacktestExecutor]:
        """Build executor from market config."""
        from .config.market_config import get_market
        config = get_market(market_name)
        cm = CostModel.from_market(config)
        executor = BacktestExecutor(cm)
        # Same executor for all instruments in this market
        return {"__default__": executor}

    def _get_executor(self, inst: str) -> BacktestExecutor | None:
        """Get executor for an instrument, falling back to __default__."""
        return self._executors.get(inst) or self._executors.get("__default__")

    def run(self) -> BacktestResult:
        """Execute the backtest. Returns BacktestResult."""
        cash = self._initial_balance
        positions: dict[str, _PositionState] = {}
        trades: list[TradeResult] = []
        equity_curve: list[EquitySnapshot] = []

        for step, ts in enumerate(self._timeline):
            cross = self._time_groups.get_group(ts)
            bars = self._build_bars(cross)

            mtm = self._calc_portfolio_value(cash, positions, bars)
            equity_curve.append(EquitySnapshot(ts=ts, equity=mtm))

            primary_inst = self._instruments[0]
            ctx = Context(
                ts=ts,
                instrument=primary_inst,
                instruments=self._instruments,
                bar=bars.get(primary_inst, {}),
                bars=bars,
                positions=self._build_position_snapshot(positions, bars),
                cash=cash,
                bar_index=step,
            )

            actions = self._strategy.on_bar(ctx)

            for action in actions:
                inst = action.instrument or primary_inst
                executor = self._get_executor(inst)
                if executor is None:
                    logger.warning("No executor for %s, skipping action", inst)
                    continue

                bar_data = bars.get(inst)
                price = bar_data["close"] if bar_data is not None else 0.0
                if price <= 0:
                    logger.warning("Invalid price %s for %s, skipping", price, inst)
                    continue

                if action.type in ("buy", "sell") and inst not in positions:
                    fill = executor.execute(action, price, cash)
                    if fill and fill.quantity > 0:
                        cash -= executor.cost_model.estimate_entry_outlay(price, fill.quantity)
                        positions[inst] = _PositionState(
                            instrument=inst,
                            side=fill.side,
                            entry_price=price,
                            quantity=fill.quantity,
                            entry_ts=ts,
                            bars_held=0,
                            entry_commission=fill.commission,
                            entry_slippage=fill.slippage,
                        )

                elif action.type == "close" and inst in positions:
                    pos = positions[inst]
                    cm = executor.cost_model
                    trade, proceeds = self._close_position(pos, ts, price, cm)
                    trades.append(trade)
                    cash += proceeds
                    del positions[inst]

            self._increment_bars_held(positions)

        # Force-close all open positions at last bar
        if self._timeline:
            last_ts = self._timeline[-1]
            last_cross = self._time_groups.get_group(last_ts)
            last_bars = self._build_bars(last_cross)
            for inst in list(positions.keys()):
                pos = positions[inst]
                last_bar = last_bars.get(inst)
                price = last_bar["close"] if last_bar is not None else pos.entry_price
                executor = self._get_executor(inst)
                cm = executor.cost_model if executor else CostModel.zero()
                trade, proceeds = self._close_position(pos, last_ts, price, cm)
                trades.append(trade)
                cash += proceeds
                del positions[inst]

        # Compute benchmark
        benchmark_curve = self._compute_benchmark()

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            initial_balance=self._initial_balance,
            final_equity=cash,
            benchmark_curve=benchmark_curve,
        )

    def _compute_benchmark(self) -> list[float] | None:
        """Compute benchmark equity curve based on self._benchmark setting."""
        if self._benchmark is None:
            return None

        if isinstance(self._benchmark, pd.Series):
            # User-provided return series → cumulative equity
            cum = (1 + self._benchmark).cumprod() * self._initial_balance
            return cum.tolist()

        # "auto": buy-and-hold for single asset only
        if self._benchmark == "auto" and len(self._instruments) == 1:
            inst = self._instruments[0]
            closes = []
            for ts in self._timeline:
                cross = self._time_groups.get_group(ts)
                bars = self._build_bars(cross)
                bar = bars.get(inst)
                if bar is not None:
                    closes.append(bar["close"])
            if closes and closes[0] > 0:
                return [self._initial_balance * (c / closes[0]) for c in closes]

        return None

    @staticmethod
    def _build_bars(cross: pd.DataFrame) -> dict[str, dict[str, float]]:
        """Convert cross-section DataFrame to {instrument: {col: val}} dict."""
        raw = cross.to_dict(orient="index")
        return {(k[0] if isinstance(k, tuple) else k): v for k, v in raw.items()}

    @staticmethod
    def _increment_bars_held(positions: dict[str, _PositionState]) -> None:
        for ps in positions.values():
            ps.bars_held += 1

    def _calc_portfolio_value(
        self,
        cash: float,
        positions: dict[str, _PositionState],
        bars: dict[str, dict[str, float]],
    ) -> float:
        mtm = cash
        for inst, pos in positions.items():
            bar = bars.get(inst)
            price = bar["close"] if bar is not None else pos.entry_price
            executor = self._get_executor(inst)
            cm = executor.cost_model if executor else CostModel.zero()
            mtm += cm.calc_pnl(pos.entry_price, price, pos.quantity) * pos.direction
            mtm += pos.entry_price * pos.quantity * cm.multiplier
        return mtm

    def _build_position_snapshot(
        self,
        positions: dict[str, _PositionState],
        bars: dict[str, dict[str, float]],
    ) -> dict[str, Position]:
        """Convert mutable _PositionState to frozen Position for Context."""
        snapshot: dict[str, Position] = {}
        for inst, ps in positions.items():
            bar = bars.get(inst)
            price = bar["close"] if bar is not None else ps.entry_price
            executor = self._get_executor(inst)
            cm = executor.cost_model if executor else CostModel.zero()
            snapshot[inst] = Position(
                instrument=inst,
                side=ps.side,
                entry_price=ps.entry_price,
                quantity=ps.quantity,
                entry_ts=ps.entry_ts,
                bars_held=ps.bars_held,
                unrealized_pnl=cm.calc_pnl(ps.entry_price, price, ps.quantity) * ps.direction,
            )
        return snapshot

    @staticmethod
    def _close_position(
        pos: _PositionState,
        exit_ts: datetime,
        exit_price: float,
        cm: CostModel,
    ) -> tuple[TradeResult, float]:
        """Close a position. Returns (TradeResult, cash_proceeds)."""
        gross_pnl = cm.calc_pnl(pos.entry_price, exit_price, pos.quantity) * pos.direction
        exit_commission = cm.calc_commission(exit_price, pos.quantity)
        exit_slippage = cm.calc_slippage(pos.quantity)
        exit_tax = cm.calc_tax(exit_price, pos.quantity, is_sell=True)

        total_commission = pos.entry_commission + exit_commission
        total_slippage = pos.entry_slippage + exit_slippage
        net_pnl = gross_pnl - total_commission - total_slippage - exit_tax

        entry_notional = pos.entry_price * pos.quantity * cm.multiplier
        gross_return = (gross_pnl / entry_notional * 100) if entry_notional > EPSILON else 0.0
        net_return = (net_pnl / entry_notional * 100) if entry_notional > EPSILON else 0.0

        notional = exit_price * pos.quantity * cm.multiplier
        proceeds = notional - exit_commission - exit_slippage - exit_tax

        trade = TradeResult(
            instrument=pos.instrument,
            entry_ts=pos.entry_ts,
            exit_ts=exit_ts,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            gross_pnl=gross_pnl,
            commission=total_commission,
            slippage=total_slippage,
            tax=exit_tax,
            net_pnl=net_pnl,
            gross_return=gross_return,
            net_return=net_return,
            holding_bars=pos.bars_held,
        )
        return trade, proceeds
