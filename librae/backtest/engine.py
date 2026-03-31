"""Backtest engine — bar-by-bar execution with Strategy + Executor pattern.

Usage:
    from librae.config import get_market

    bt = Backtest(data=df, strategy=my_strategy, market_config=get_market("crypto"))
    bt.add_benchmark(df.xs("BTCUSDT", level="instrument")["close"])
    bt.run()
    output = bt.build_output(annualize=True)

The engine owns all position state. Strategies only observe via Context.
Execution uses make_fill() from core.executor.
Shared PnL calculation uses calc_trade_pnl() from core.executor.

Data format: MultiIndex DataFrame (instrument, datetime) with OHLCV + features.
Single-asset is a special case where instruments has one element.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Sequence

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from librae.backtest.schema import BacktestOutput, EquityCurvePoint, StrategyMetrics

from librae.config.market_config import MarketConfig
from librae.core import EPSILON
from librae.core.cost_model import CostModel
from librae.core.executor import TradePnL, calc_trade_pnl, direction, make_fill
from librae.core.strategy import Action, BaseStrategy, Context, Fill, Position
from librae.core.utils import generate_run_id, infer_timeframe, make_trade_id, to_ccxt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeResult:
    """Single completed trade from the engine."""

    instrument: str
    entry_ts: datetime
    exit_ts: datetime
    side: Literal["long", "short"]
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


# ---------------------------------------------------------------------------
# Internal mutable position state
# ---------------------------------------------------------------------------


@dataclass
class _PositionState:
    instrument: str
    side: Literal["long", "short"]
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
              For single-asset, wrap with: pd.MultiIndex.from_arrays([["SYM"]*len(df), df.index])
        strategy: BaseStrategy subclass.
        market_config: MarketConfig for cost model (mutually exclusive with cost_model).
        initial_balance: Starting cash.
        cost_model: CostModel directly (for tests or custom cost models).
    """

    def __init__(
        self,
        data: pd.DataFrame,
        strategy: BaseStrategy,
        market_config: MarketConfig | None = None,
        initial_balance: float = 100_000.0,
        *,
        cost_model: CostModel | None = None,
    ) -> None:
        self._data = data
        self._strategy = strategy
        self._initial_balance = initial_balance
        self._benchmark_prices: pd.Series | None = None
        self._run_id: str | None = None
        self._result: BacktestResult | None = None
        self._metrics: StrategyMetrics | None = None

        if not isinstance(data.index, pd.MultiIndex):
            raise ValueError(
                "data must have MultiIndex (instrument, datetime). "
                "For single-asset: df.index = pd.MultiIndex.from_arrays([['SYM']*len(df), df.index])"
            )

        self._instruments = data.index.get_level_values(0).unique().tolist()
        self._time_groups = data.groupby(level="datetime")
        self._timeline = sorted(self._time_groups.groups.keys())

        if cost_model is not None:
            cm = cost_model
        elif market_config is not None:
            cm = CostModel.from_market(market_config)
        else:
            cm = CostModel.zero()
        self._cost_models: dict[str, CostModel] = {"__default__": cm}

        # Auto-derive strategy_name from class name → snake_case
        cls_name = type(strategy).__name__
        self._strategy_name = re.sub(r"(?<!^)(?=[A-Z])", "_", cls_name).lower()

    # --- Public properties ---

    @property
    def result(self) -> BacktestResult:
        """Access BacktestResult. Raises RuntimeError if run() not called."""
        if self._result is None:
            raise RuntimeError("Call run() before accessing result")
        return self._result

    @property
    def run_id(self) -> str:
        """Access run_id. Raises RuntimeError if run() not called."""
        if self._run_id is None:
            raise RuntimeError("Call run() before accessing run_id")
        return self._run_id

    # --- Builder methods ---

    def add_benchmark(self, prices: pd.Series) -> None:
        """Set benchmark for comparison.

        Args:
            prices: Price series indexed by datetime. Engine computes
                    buy-and-hold equity curve in build_output(), aligned
                    to backtest timeline.
        """
        self._benchmark_prices = prices

    # --- Private helpers ---

    def _get_cost_model(self, inst: str) -> CostModel:
        """Get cost model for an instrument, falling back to __default__."""
        return self._cost_models.get(inst) or self._cost_models.get("__default__", CostModel.zero())

    def run(self) -> BacktestResult:
        """Execute the backtest. Generates run_id at start. Returns BacktestResult."""
        self._run_id = generate_run_id(self._strategy_name, self._instruments[0])
        logger.info("Backtest started: run_id=%s", self._run_id)

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
                cm = self._get_cost_model(inst)
                bar_data = bars.get(inst)
                price = bar_data["close"] if bar_data is not None else 0.0
                if price <= 0:
                    logger.warning("Invalid price %s for %s, skipping", price, inst)
                    continue

                if action.type in ("buy", "sell") and inst not in positions:
                    fill = make_fill(action, price, cash, cm)
                    if fill and fill.quantity > 0:
                        cash -= cm.estimate_entry_outlay(price, fill.quantity)
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
                cm = self._get_cost_model(inst)
                trade, proceeds = self._close_position(pos, last_ts, price, cm)
                trades.append(trade)
                cash += proceeds
                del positions[inst]

        self._result = BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            initial_balance=self._initial_balance,
            final_equity=cash,
        )
        return self._result

    def build_output(
        self,
        *,
        annualize: bool = False,
    ) -> "BacktestOutput":
        """Compute metrics + build canonical output in one call.

        All metadata is auto-derived from the engine state:
        - run_id: generated in run()
        - start_ts/end_ts: from self._timeline
        - symbol: from self._instruments[0]
        - strategy_name: from type(strategy).__name__
        - timeframe: inferred from data index

        Raises RuntimeError if called before run().
        """
        from librae.backtest.schema import (
            BacktestOutput, EquityCurvePoint, RunMetadata, TradeRecord,
        )
        from librae.core.metrics import compute_all

        if self._result is None:
            raise RuntimeError("Call run() before build_output()")

        result = self._result
        run_id = self._run_id

        timeline = self._timeline
        start_ts = timeline[0].to_pydatetime() if hasattr(timeline[0], "to_pydatetime") else timeline[0]
        end_ts = timeline[-1].to_pydatetime() if hasattr(timeline[-1], "to_pydatetime") else timeline[-1]
        symbol = self._instruments[0]
        # WHY: reuse self._timeline (already sorted) instead of re-extracting from index
        timeframe = infer_timeframe(pd.DatetimeIndex(timeline))

        # Benchmark — computed here, not in run() (analysis config, not trade facts)
        benchmark_curve = self._compute_benchmark()

        # Build TradePnL list + holding_bars from TradeResult
        trade_pnl_list = [
            TradePnL(
                gross_pnl=t.gross_pnl, net_pnl=t.net_pnl,
                commission=t.commission, slippage=t.slippage, tax=t.tax,
                gross_return=t.gross_return, net_return=t.net_return,
                exit_commission=0.0, exit_slippage=0.0, exit_tax=t.tax,
            )
            for t in result.trades
        ]
        trade_holding_bars = [t.holding_bars for t in result.trades]

        self._metrics = compute_all(
            equity_values=[s.equity for s in result.equity_curve],
            timestamps=[s.ts for s in result.equity_curve],
            trade_pnls=trade_pnl_list,
            total_bars=len(result.equity_curve),
            annualize=annualize,
            benchmark_values=benchmark_curve,
            holding_bars=trade_holding_bars,
        )

        run_metadata = RunMetadata(
            run_id=run_id,
            strategy=self._strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            start_ts=start_ts,
            end_ts=end_ts,
            run_ts=datetime.now(tz=timezone.utc),
        )

        trade_records = self._build_trade_records(result, run_id)
        equity_points = self._enrich_equity_curve(result, benchmark_curve)

        return BacktestOutput(
            run_metadata=run_metadata,
            equity_curve=equity_points,
            trades=trade_records,
            metrics=self._metrics,
        )

    @property
    def metrics(self) -> "StrategyMetrics":
        """Access StrategyMetrics. Raises RuntimeError if build_output not called."""
        if self._metrics is None:
            raise RuntimeError("Call build_output() before accessing metrics")
        return self._metrics

    @staticmethod
    def _build_trade_records(result: BacktestResult, run_id: str) -> list:
        """Map TradeResult → TradeRecord."""
        from librae.backtest.schema import TradeRecord
        return [
            TradeRecord(
                trade_id=make_trade_id(run_id, i),
                entry_ts=t.entry_ts, exit_ts=t.exit_ts,
                symbol=t.instrument, side=t.side,
                entry_price=float(t.entry_price), exit_price=float(t.exit_price),
                quantity=float(t.quantity),
                gross_pnl=float(t.gross_pnl), net_pnl=float(t.net_pnl),
                gross_return=float(t.gross_return), net_return=float(t.net_return),
                commission=float(t.commission), slippage=float(t.slippage),
                holding_bars=int(t.holding_bars),
            )
            for i, t in enumerate(result.trades)
        ]

    @staticmethod
    def _enrich_equity_curve(
        result: BacktestResult,
        benchmark_curve: list[float] | None,
    ) -> list[EquityCurvePoint]:
        """Build EquityCurvePoints with drawdown, ret_1d, and benchmark alignment."""
        from librae.backtest.schema import EquityCurvePoint

        has_bm = benchmark_curve is not None and len(benchmark_curve) > 0
        equity_points: list[EquityCurvePoint] = []
        peak = 0.0
        prev_eq = result.equity_curve[0].equity if result.equity_curve else 1.0
        prev_bm = 1.0
        for i, snap in enumerate(result.equity_curve):
            eq = snap.equity
            peak = max(peak, eq)
            drawdown = (eq - peak) / peak if peak > 0 else 0.0
            ret_1d = (eq / prev_eq - 1.0) if prev_eq > 0 else 0.0
            prev_eq = eq

            bm_eq, bm_ret = None, None
            if has_bm and i < len(benchmark_curve):
                bm_eq = float(benchmark_curve[i])
                bm_ret = (bm_eq / prev_bm - 1.0) if prev_bm > 0 else 0.0
                prev_bm = bm_eq

            equity_points.append(EquityCurvePoint(
                ts=snap.ts, equity=float(eq),
                ret_1d=float(ret_1d), drawdown=float(drawdown),
                benchmark_equity=bm_eq, benchmark_ret_1d=bm_ret,
            ))
        return equity_points

    def _compute_benchmark(self) -> list[float] | None:
        """Compute benchmark buy-and-hold equity curve from benchmark prices."""
        if self._benchmark_prices is None:
            return None
        prices = self._benchmark_prices
        if len(prices) == 0 or prices.iloc[0] <= 0:
            return None
        return [self._initial_balance * (p / prices.iloc[0]) for p in prices]

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
            cm = self._get_cost_model(inst)
            mtm += cm.calc_pnl(pos.entry_price, price, pos.quantity) * direction(pos.side)
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
            cm = self._get_cost_model(inst)
            snapshot[inst] = Position(
                instrument=inst,
                side=ps.side,
                entry_price=ps.entry_price,
                quantity=ps.quantity,
                entry_ts=ps.entry_ts,
                bars_held=ps.bars_held,
                unrealized_pnl=cm.calc_pnl(ps.entry_price, price, ps.quantity) * direction(ps.side),
            )
        return snapshot

    @staticmethod
    def _close_position(
        pos: _PositionState,
        exit_ts: datetime,
        exit_price: float,
        cm: CostModel,
    ) -> tuple[TradeResult, float]:
        """Close a position using shared calc_trade_pnl. Returns (TradeResult, cash_proceeds)."""
        pnl = calc_trade_pnl(
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            side=pos.side,
            cost_model=cm,
            entry_commission=pos.entry_commission,
            entry_slippage=pos.entry_slippage,
        )

        notional = exit_price * pos.quantity * cm.multiplier
        proceeds = notional - pnl.exit_commission - pnl.exit_slippage - pnl.exit_tax

        trade = TradeResult(
            instrument=pos.instrument,
            entry_ts=pos.entry_ts,
            exit_ts=exit_ts,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            gross_pnl=pnl.gross_pnl,
            commission=pnl.commission,
            slippage=pnl.slippage,
            tax=pnl.tax,
            net_pnl=pnl.net_pnl,
            gross_return=pnl.gross_return,
            net_return=pnl.net_return,
            holding_bars=pos.bars_held,
        )
        return trade, proceeds
