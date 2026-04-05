"""Backtest engine — bar-by-bar execution with Strategy + Executor pattern.

Usage:
    from librae.config import get_market

    bt = Backtest(data=df, strategy=my_strategy, market_config=get_market("crypto"))
    bt.add_benchmark(df.xs("BTCUSDT", level="symbol")["close"])
    bt.run()
    output = bt.build_output(annualize=True)

The engine owns all position state. Strategies only observe via Context.
Execution uses make_fill() from core.executor.
Shared PnL calculation uses calc_trade_pnl() from core.executor.

Data format: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
Single-asset is a special case where symbols has one element.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Sequence

import pandas as pd

if TYPE_CHECKING:
    from librae.backtest.schema import BacktestOutput, EquityCurvePoint, StrategyMetrics

from librae.config.market_config import MarketConfig
from librae.core import EPSILON
from librae.core.cost_model import CostModel
from librae.core.executor import (
    OrderEvent, TradePnL, TradeResult, build_trade_result,
    close_position, direction, process_actions,
)
from librae.core.strategy import Action, BaseStrategy, Context, Fill, Position, PositionState
from librae.core.utils import generate_run_id, infer_timeframe, make_event_id, make_trade_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EquitySnapshot:
    """Single bar equity snapshot."""

    ts: datetime
    equity: float


@dataclass(frozen=True)
class BacktestResult:
    """Raw output from engine — no metrics, just facts."""

    trades: Sequence[TradeResult]
    order_events: Sequence[OrderEvent]
    equity_curve: Sequence[EquitySnapshot]
    initial_balance: float
    final_equity: float
    exposed_bars: int = 0


# ---------------------------------------------------------------------------
# Backtest class
# ---------------------------------------------------------------------------


class Backtest:
    """Bar-by-bar backtest engine.

    Args:
        data: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
              For single-asset, wrap with: pd.MultiIndex.from_arrays([["SYM"]*len(df), df.index])
        strategy: BaseStrategy subclass.
        market_config: MarketConfig for cost model (mutually exclusive with cost_model).
        initial_balance: Starting cash.
        strategy_name: Override strategy name (default: snake_case of class name).
        cost_model: CostModel directly (for tests or custom cost models).
    """

    def __init__(
        self,
        data: pd.DataFrame,
        strategy: BaseStrategy,
        market_config: MarketConfig | None = None,
        initial_balance: float = 100_000.0,
        *,
        strategy_name: str | None = None,
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
                "data must have MultiIndex (symbol, datetime). "
                "For single-asset: df.index = pd.MultiIndex.from_arrays([['SYM']*len(df), df.index])"
            )

        self._symbols = data.index.get_level_values(0).unique().tolist()
        self._time_groups = data.groupby(level="datetime")
        self._timeline = sorted(self._time_groups.groups.keys())

        if cost_model is not None:
            resolved_cm = cost_model
        elif market_config is not None:
            resolved_cm = CostModel.from_market(market_config)
        else:
            resolved_cm = CostModel.zero()
        self._cost_models: dict[str, CostModel] = {"__default__": resolved_cm}

        if strategy_name is not None:
            self._strategy_name = strategy_name
        else:
            # Fallback: auto-derive from class name → snake_case
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

    def _get_cost_model(self, symbol: str) -> CostModel:
        """Get cost model for a symbol, falling back to __default__."""
        return self._cost_models.get(symbol) or self._cost_models.get("__default__", CostModel.zero())

    def run(self) -> BacktestResult:
        """Execute the backtest. Generates run_id at start. Returns BacktestResult."""
        self._run_id = generate_run_id(self._strategy_name, self._symbols[0])
        logger.info("Backtest started: run_id=%s", self._run_id)

        # WHY: pre-convert all bars once — avoids per-bar DataFrame.to_dict()
        # which is the dominant cost in the hot loop.
        all_bars = self._precompute_bars()

        cash = self._initial_balance
        positions: dict[str, PositionState] = {}
        trades: list[TradeResult] = []
        all_events: list[OrderEvent] = []
        equity_curve: list[EquitySnapshot] = []
        exposed_bars = 0
        primary_symbol = self._symbols[0]

        for step, ts in enumerate(self._timeline):
            bars = all_bars[ts]

            mtm, pos_snapshot = self._eval_equity(cash, positions, bars)
            equity_curve.append(EquitySnapshot(ts=ts, equity=mtm))

            if positions:
                exposed_bars += 1

            ctx = Context(
                ts=ts,
                symbol=primary_symbol,
                symbols=self._symbols,
                bar=bars.get(primary_symbol, {}),
                bars=bars,
                positions=pos_snapshot,
                cash=cash,
                bar_index=step,
            )

            actions = self._strategy.on_bar(ctx)

            result_actions = process_actions(
                actions, positions, cash, ts,
                get_price=lambda sym: bars.get(sym, {}).get("close"),
                get_cost_model=self._get_cost_model,
                primary_symbol=primary_symbol,
            )
            trades.extend(result_actions.trades)
            all_events.extend(result_actions.events)
            cash += result_actions.cash_delta

            self._increment_bars_held(positions)

        # Force-close all open positions at last bar
        if self._timeline:
            last_ts = self._timeline[-1]
            last_bars = all_bars[last_ts]
            for sym in list(positions.keys()):
                pos = positions[sym]
                last_bar = last_bars.get(sym)
                price = last_bar["close"] if last_bar is not None else pos.entry_price
                cost_model = self._get_cost_model(sym)
                pnl, proceeds, _ = close_position(pos, price, cost_model)
                trades.append(build_trade_result(pos, last_ts, price, pos.quantity, pnl))
                all_events.append(OrderEvent(
                    ts=last_ts, symbol=sym, side=pos.side, event_type="close",
                    quantity=pos.quantity, price=price,
                    avg_entry_price=pos.entry_price, position_qty=0.0,
                    notional=price * pos.quantity * cost_model.multiplier,
                    commission=pnl.commission, slippage=pnl.slippage, tax=pnl.tax,
                    realized_pnl=pnl.net_pnl, net_return=pnl.net_return,
                    entry_ts=pos.entry_ts, holding_bars=pos.bars_held,
                    reason="force_close",
                ))
                cash += proceeds
                del positions[sym]

        self._result = BacktestResult(
            trades=trades,
            order_events=all_events,
            equity_curve=equity_curve,
            initial_balance=self._initial_balance,
            final_equity=cash,
            exposed_bars=exposed_bars,
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
        - symbol: from self._symbols[0]
        - strategy_name: from type(strategy).__name__
        - timeframe: inferred from data index

        Raises RuntimeError if called before run().
        """
        from librae.backtest.schema import (
            BacktestOutput, EquityCurvePoint, OrderEventRecord, RunMetadata, TradeRecord,
        )
        from librae.core.metrics import compute_all

        if self._result is None:
            raise RuntimeError("Call run() before build_output()")

        result = self._result
        run_id = self._run_id

        timeline = self._timeline
        start_ts = timeline[0].to_pydatetime() if hasattr(timeline[0], "to_pydatetime") else timeline[0]
        end_ts = timeline[-1].to_pydatetime() if hasattr(timeline[-1], "to_pydatetime") else timeline[-1]
        symbol = self._symbols[0]
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
        trade_quantities = [t.quantity for t in result.trades]

        self._metrics = compute_all(
            equity_values=[s.equity for s in result.equity_curve],
            timestamps=[s.ts for s in result.equity_curve],
            trade_pnls=trade_pnl_list,
            total_bars=len(result.equity_curve),
            annualize=annualize,
            benchmark_values=benchmark_curve,
            exposed_bars=result.exposed_bars,
            trade_quantities=trade_quantities,
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
        event_records = self._build_event_records(result, run_id)
        equity_points = self._enrich_equity_curve(result, benchmark_curve)

        return BacktestOutput(
            run_metadata=run_metadata,
            equity_curve=tuple(equity_points),
            trades=tuple(trade_records),
            order_events=tuple(event_records),
            metrics=self._metrics,
        )

    @property
    def metrics(self) -> "StrategyMetrics":
        """Access StrategyMetrics. Raises RuntimeError if build_output not called."""
        if self._metrics is None:
            raise RuntimeError("Call build_output() before accessing metrics")
        return self._metrics

    @staticmethod
    def _build_trade_records(result: BacktestResult, run_id: str) -> list[TradeRecord]:
        """Map TradeResult → TradeRecord."""
        from librae.backtest.schema import TradeRecord
        return [
            TradeRecord(
                trade_id=make_trade_id(run_id, i),
                entry_ts=t.entry_ts, exit_ts=t.exit_ts,
                symbol=t.symbol, side=t.side,
                entry_price=float(t.entry_price), exit_price=float(t.exit_price),
                quantity=float(t.quantity),
                gross_pnl=float(t.gross_pnl), net_pnl=float(t.net_pnl),
                gross_return=float(t.gross_return), net_return=float(t.net_return),
                commission=float(t.commission), slippage=float(t.slippage),
                tax=float(t.tax), holding_bars=int(t.holding_bars),
            )
            for i, t in enumerate(result.trades)
        ]

    @staticmethod
    def _build_event_records(result: BacktestResult, run_id: str) -> list["OrderEventRecord"]:
        """Map OrderEvent → OrderEventRecord."""
        from librae.backtest.schema import OrderEventRecord
        return [
            OrderEventRecord(
                event_id=make_event_id(run_id, i),
                ts=e.ts, symbol=e.symbol, side=e.side, event_type=e.event_type,
                quantity=float(e.quantity), price=float(e.price),
                avg_entry_price=float(e.avg_entry_price),
                position_qty=float(e.position_qty),
                notional=float(e.notional),
                commission=float(e.commission), slippage=float(e.slippage),
                tax=float(e.tax),
                realized_pnl=float(e.realized_pnl) if e.realized_pnl is not None else None,
                net_return=float(e.net_return) if e.net_return is not None else None,
                entry_ts=e.entry_ts, holding_bars=e.holding_bars,
                reason=e.reason,
            )
            for i, e in enumerate(result.order_events)
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
        prev_bm = float(benchmark_curve[0]) if has_bm else 1.0
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

    def _precompute_bars(self) -> dict[pd.Timestamp, dict[str, dict[str, float]]]:
        """Pre-convert all cross-sections to dicts once.

        Eliminates per-bar DataFrame.to_dict() calls in the hot loop.
        Trades O(N_bars) memory for O(1) per-bar lookup.
        """
        result: dict[pd.Timestamp, dict[str, dict[str, float]]] = {}
        for ts, cross in self._time_groups:
            raw = cross.to_dict(orient="index")
            result[ts] = {(k[0] if isinstance(k, tuple) else k): v for k, v in raw.items()}
        return result

    @staticmethod
    def _increment_bars_held(positions: dict[str, PositionState]) -> None:
        for ps in positions.values():
            ps.bars_held += 1

    def _eval_equity(
        self,
        cash: float,
        positions: dict[str, PositionState],
        bars: dict[str, dict[str, float]],
    ) -> tuple[float, dict[str, Position]]:
        """Compute portfolio MTM value and position snapshot in a single pass.

        Returns (mark_to_market, {symbol: Position}).
        """
        mtm = cash
        snapshot: dict[str, Position] = {}
        for sym, ps in positions.items():
            bar = bars.get(sym)
            price = bar["close"] if bar is not None else ps.entry_price
            cost_model = self._get_cost_model(sym)
            unrealized = cost_model.calc_pnl(ps.entry_price, price, ps.quantity) * direction(ps.side)
            mtm += unrealized + ps.entry_price * ps.quantity * cost_model.multiplier
            snapshot[sym] = Position(
                symbol=sym,
                side=ps.side,
                entry_price=ps.entry_price,
                quantity=ps.quantity,
                entry_ts=ps.entry_ts,
                bars_held=ps.bars_held,
                unrealized_pnl=unrealized,
            )
        return mtm, snapshot

