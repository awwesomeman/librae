"""Backtest engine — bar-by-bar execution with Strategy + Executor pattern.

Usage:
    bt = Backtest(data=df, strategy=my_strategy, initial_balance=100_000)
    result = bt.run()

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
# Output dataclasses (unchanged from v1)
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
    entry_commission: float
    entry_slippage: float


# ---------------------------------------------------------------------------
# Backtest class
# ---------------------------------------------------------------------------


class Backtest:
    """Bar-by-bar backtest engine.

    Args:
        data: MultiIndex DataFrame (instrument, datetime) with OHLCV + features.
              For single-asset, wrap with: df.index = pd.MultiIndex.from_product([["SYM"], df.index])
        strategy: BaseStrategy subclass.
        initial_balance: Starting cash.
        warmup_bars: Number of timeline steps to skip before calling strategy.
        executor: Optional custom Executor. If None, auto-builds from markets.yaml.
    """

    def __init__(
        self,
        data: pd.DataFrame,
        strategy: BaseStrategy,
        initial_balance: float = 100_000.0,
        warmup_bars: int = 0,
        executor: BacktestExecutor | None = None,
    ) -> None:
        self._data = data
        self._strategy = strategy
        self._initial_balance = initial_balance
        self._warmup_bars = warmup_bars

        # Parse instruments from MultiIndex
        if not isinstance(data.index, pd.MultiIndex):
            raise ValueError(
                "data must have MultiIndex (instrument, datetime). "
                "For single-asset: df.index = pd.MultiIndex.from_arrays([['SYM']*len(df), df.index])"
            )

        self._instruments = data.index.get_level_values(0).unique().tolist()
        self._time_groups = data.groupby(level="datetime")
        self._timeline = sorted(self._time_groups.groups.keys())

        # Build executors per instrument
        if executor is not None:
            self._executors = {inst: executor for inst in self._instruments}
        else:
            self._executors = self._auto_build_executors()

    def _auto_build_executors(self) -> dict[str, BacktestExecutor]:
        """Build BacktestExecutor per instrument from markets.yaml."""
        from config.market_config import load_market_configs
        configs = load_market_configs()

        # Build symbol → instrument_id mapping
        symbol_to_id = {cfg.symbol: inst_id for inst_id, cfg in configs.items()}

        executors: dict[str, BacktestExecutor] = {}
        for inst in self._instruments:
            inst_id = symbol_to_id.get(inst)
            if inst_id and inst_id in configs:
                cm = CostModel.from_instrument(configs[inst_id])
            else:
                logger.warning(
                    "No config for instrument %s, using zero-cost model", inst,
                )
                cm = CostModel.zero()
            executors[inst] = BacktestExecutor(cm)

        return executors

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

            if step < self._warmup_bars:
                self._increment_bars_held(positions)
                continue

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
                executor = self._executors.get(inst)
                if executor is None:
                    continue

                bar_data = bars.get(inst)
                price = bar_data["close"] if bar_data is not None else 0.0
                if price <= 0:
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
                cm = self._executors[inst].cost_model
                trade, proceeds = self._close_position(pos, last_ts, price, cm)
                trades.append(trade)
                cash += proceeds
                del positions[inst]

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            initial_balance=self._initial_balance,
            final_equity=cash,
        )

    # --- Private helpers ---

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
            cm = self._executors[inst].cost_model
            mtm += cm.calc_pnl(pos.entry_price, price, pos.quantity)
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
            cm = self._executors[inst].cost_model
            snapshot[inst] = Position(
                instrument=inst,
                side=ps.side,
                entry_price=ps.entry_price,
                quantity=ps.quantity,
                entry_ts=ps.entry_ts,
                bars_held=ps.bars_held,
                unrealized_pnl=cm.calc_pnl(ps.entry_price, price, ps.quantity),
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
        gross_pnl = cm.calc_pnl(pos.entry_price, exit_price, pos.quantity)
        exit_commission = cm.calc_commission(exit_price, pos.quantity)
        exit_slippage = cm.calc_slippage(pos.quantity)
        exit_tax = cm.calc_tax(exit_price, pos.quantity, is_sell=True)

        total_commission = pos.entry_commission + exit_commission
        total_slippage = pos.entry_slippage + exit_slippage
        net_pnl = gross_pnl - total_commission - total_slippage - exit_tax

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
            holding_bars=pos.bars_held,
        )
        return trade, proceeds
