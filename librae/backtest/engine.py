"""Backtest engine — bar-by-bar execution with Strategy + Executor pattern.

Usage:
    from librae.core.run_config import RunConfig

    bt = Backtest(data=df, strategy=my_strategy, cfg=cfg)
    bt.add_benchmark(df.xs("BTCUSDT", level="symbol")["close"])
    bt.run()
    output = bt.build_output()

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
from typing import TYPE_CHECKING, Sequence

import pandas as pd

if TYPE_CHECKING:
    from librae.backtest.schema import BacktestOutput, EquityCurvePoint, StrategyMetrics
    from librae.core.run_config import RunConfig

from librae.config.market_config import MarketConfig
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_FORCE_CLOSE, OrderEvent, TradePnL, TradeResult,
    build_close_event, eval_equity, run_pending_and_stops,
)
from librae.core.strategy import Action, BaseStrategy, Context, Position, PositionState
from librae.core.utils import generate_run_id, infer_timeframe, make_event_id

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
    exposed_periods: int = 0


# ---------------------------------------------------------------------------
# Backtest class
# ---------------------------------------------------------------------------


class Backtest:
    """Bar-by-bar backtest engine.

    Args:
        data: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
              For single-asset, wrap with: pd.MultiIndex.from_arrays([["SYM"]*len(df), df.index])
        strategy: BaseStrategy subclass.
        cfg: RunConfig (preferred). Derives market_config, initial_balance, etc.
        market_config: MarketConfig for cost model (legacy, use cfg instead).
        initial_balance: Starting cash (legacy, use cfg instead).
        strategy_name: Override strategy name (default: from cfg or snake_case of class name).
        cost_model: CostModel directly (for tests or custom cost models).
        data_source: Data source identifier (legacy, use cfg instead).
    """

    def __init__(
        self,
        data: pd.DataFrame,
        strategy: BaseStrategy,
        cfg: RunConfig | None = None,
        market_config: MarketConfig | None = None,
        initial_balance: float = 100_000.0,
        *,
        strategy_name: str | None = None,
        cost_model: CostModel | None = None,
        data_source: str = "",
    ) -> None:
        self._data = data
        self._strategy = strategy
        self._cfg = cfg
        self._benchmark_prices: pd.Series | None = None
        self._run_id: str | None = None
        self._timeframe: str | None = None
        self._result: BacktestResult | None = None
        self._metrics: StrategyMetrics | None = None

        if not isinstance(data.index, pd.MultiIndex):
            raise ValueError(
                "data must have MultiIndex (symbol, datetime). "
                "For single-asset: df.index = pd.MultiIndex.from_arrays([['SYM']*len(df), df.index])"
            )

        self._symbols = data.index.get_level_values(0).unique().tolist()
        self._timeline = sorted(data.index.get_level_values("datetime").unique())

        # Resolve from cfg or explicit args
        if cfg is not None:
            self._initial_balance = cfg.initial_balance
            self._data_source = cfg.data_source
            resolved_name = cfg.strategy_name
            resolved_cm = CostModel.from_config(cfg, override=cost_model)
        else:
            self._initial_balance = initial_balance
            self._data_source = data_source
            resolved_name = None
            if cost_model is not None:
                resolved_cm = cost_model
            elif market_config is not None:
                raise ValueError(
                    "market_config alone can't build a CostModel — multiplier comes from "
                    "symbols.yaml (spot=1.0 auto, contract_* explicit-required), which "
                    "this legacy path has no symbol for. Pass cost_model= directly (e.g. "
                    "CostModel.from_market(market_config, multiplier=...)), or use cfg= instead."
                )
            else:
                resolved_cm = CostModel.zero()

        self._cost_models: dict[str, CostModel] = {"__default__": resolved_cm}
        self._fill_price: str = (cfg.params or {}).get("fill_price", "open") if cfg else "open"

        if strategy_name is not None:
            self._strategy_name = strategy_name.lower().replace(" ", "_")
        elif resolved_name:
            self._strategy_name = resolved_name
        else:
            cls_name = type(strategy).__name__
            if cls_name.endswith("Strategy") and len(cls_name) > 8:
                cls_name = cls_name[:-8]
            s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", cls_name)
            self._strategy_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()

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
        self._timeframe = infer_timeframe(pd.DatetimeIndex(self._timeline[:20]))
        self._run_id = generate_run_id(self._strategy_name, self._symbols[0], self._timeframe)
        logger.info("Backtest started: run_id=%s", self._run_id)

        # WHY: pre-convert all bars once — avoids per-bar DataFrame.to_dict()
        # which is the dominant cost in the hot loop.
        all_bars = self._precompute_bars()

        cash = self._initial_balance
        positions: dict[str, PositionState] = {}
        trades: list[TradeResult] = []
        all_events: list[OrderEvent] = []
        equity_curve: list[EquitySnapshot] = []
        exposed_periods = 0
        primary_symbol = self._symbols[0]
        pending_actions: list[Action] = []

        for step, ts in enumerate(self._timeline):
            bars = all_bars[ts]

            # ── Steps 1+1.5: fill previous bar's pending actions at current
            # bar's price, then check stop-loss/take-profit — shared with the
            # live engine (librae.core.executor.run_pending_and_stops) so the
            # two can't silently drift out of sync on this sequence ──
            cash, step_result = run_pending_and_stops(
                ts, positions, cash, pending_actions, bars,
                get_cost_model=self._get_cost_model,
                default_fill=self._fill_price,
                primary_symbol=primary_symbol,
            )
            trades.extend(step_result.trades)
            all_events.extend(step_result.events)
            pending_actions = []

            # ── Step 2: equity snapshot (reflects just-executed trades) ──
            mtm, pos_snapshot = self._eval_equity(cash, positions, bars)
            equity_curve.append(EquitySnapshot(ts=ts, equity=mtm))

            if positions:
                exposed_periods += 1

            # ── Step 3: strategy decision (produces next bar's pending actions) ──
            ctx = Context(
                ts=ts,
                symbol=primary_symbol,
                symbols=self._symbols,
                bar=bars.get(primary_symbol, {}),
                bars=bars,
                positions=pos_snapshot,
                cash=cash,
                period_index=step,
            )
            pending_actions = self._strategy.on_bar(ctx)

            self._increment_periods_held(positions)

        # WHY: pending_actions from last on_bar() are discarded — no T+1 bar to fill them.
        # Force-close all open positions at last bar
        if self._timeline:
            last_ts = self._timeline[-1]
            last_bars = all_bars[last_ts]
            for sym in list(positions.keys()):
                pos = positions[sym]
                last_bar = last_bars.get(sym)
                price = last_bar["close"] if last_bar is not None else pos.entry_price
                cost_model = self._get_cost_model(sym)
                trade, event, proceeds, _ = build_close_event(
                    pos, last_ts, price, cost_model, REASON_FORCE_CLOSE,
                )
                trades.append(trade)
                all_events.append(event)
                cash += proceeds
                del positions[sym]

        self._result = BacktestResult(
            trades=trades,
            order_events=all_events,
            equity_curve=equity_curve,
            initial_balance=self._initial_balance,
            final_equity=cash,
            exposed_periods=exposed_periods,
        )
        return self._result

    def build_output(
        self,
        *,
        annualize: bool | None = None,
    ) -> "BacktestOutput":
        """Compute metrics + build canonical output in one call.

        All metadata is auto-derived from the engine state:
        - run_id: generated in run()
        - started_at/ended_at: from self._timeline
        - symbol: from self._symbols[0]
        - strategy_name: from type(strategy).__name__
        - timeframe: inferred from data index

        When cfg is provided, perf_params come from cfg.
        annualize kwarg overrides cfg.annualize if explicitly passed.

        Raises RuntimeError if called before run().
        """
        from librae.backtest.schema import (
            BacktestOutput, RunMetadata,
        )
        from librae.core.metrics import compute_all

        if self._result is None:
            raise RuntimeError("Call run() before build_output()")

        result = self._result
        run_id = self._run_id

        timeline = self._timeline
        started_at = timeline[0].to_pydatetime() if hasattr(timeline[0], "to_pydatetime") else timeline[0]
        ended_at = timeline[-1].to_pydatetime() if hasattr(timeline[-1], "to_pydatetime") else timeline[-1]
        symbol = self._symbols[0]
        timeframe = self._timeframe

        # Benchmark — computed here, not in run() (analysis config, not trade facts)
        benchmark_curve = self._compute_benchmark()

        # Build TradePnL list + periods_held from TradeResult
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

        # Resolve perf params from cfg or explicit args
        perf_kwargs: dict = {}
        if self._cfg is not None:
            perf_kwargs = self._cfg.perf_params.copy()
        if annualize is not None:
            perf_kwargs["annualize"] = annualize
        elif "annualize" not in perf_kwargs:
            perf_kwargs["annualize"] = False

        self._metrics = compute_all(
            equity_values=[s.equity for s in result.equity_curve],
            timestamps=[s.ts for s in result.equity_curve],
            trade_pnls=trade_pnl_list,
            total_periods=len(result.equity_curve),
            benchmark_values=benchmark_curve,
            exposed_periods=result.exposed_periods,
            trade_quantities=trade_quantities,
            **perf_kwargs,
        )

        run_metadata = RunMetadata(
            run_id=run_id,
            strategy=self._strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            data_source=self._data_source,
            started_at=started_at,
            ended_at=ended_at,
            run_at=datetime.now(tz=timezone.utc),
        )

        event_records = self._build_event_records(result, run_id)
        equity_points = self._enrich_equity_curve(result, benchmark_curve)

        return BacktestOutput(
            run_metadata=run_metadata,
            equity_curve=tuple(equity_points),
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
    def _build_event_records(result: BacktestResult, run_id: str) -> list["OrderEventRecord"]:
        """Map OrderEvent -> OrderEventRecord."""
        from librae.backtest.schema import OrderEventRecord
        return [
            OrderEventRecord(
                event_id=make_event_id(run_id, i),
                ts=e.ts, symbol=e.symbol, side=e.side, event_type=e.event_type,
                fill_quantity=float(e.fill_quantity), price=float(e.price),
                entry_price=float(e.entry_price),
                remaining_quantity=float(e.remaining_quantity),
                notional=float(e.notional),
                commission=float(e.commission), slippage=float(e.slippage),
                tax=float(e.tax),
                pnl=float(e.pnl) if e.pnl is not None else None,
                net_return=float(e.net_return) if e.net_return is not None else None,
                entry_at=e.entry_at, periods_held=e.periods_held,
                reason=e.reason,
            )
            for i, e in enumerate(result.order_events)
        ]

    @staticmethod
    def _enrich_equity_curve(
        result: BacktestResult,
        benchmark_curve: list[float] | None,
    ) -> list[EquityCurvePoint]:
        """Build EquityCurvePoints with drawdown, period_return, and benchmark alignment."""
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
            period_return = (eq / prev_eq - 1.0) if prev_eq > 0 else 0.0
            prev_eq = eq

            bm_eq, bm_ret = None, None
            if has_bm and i < len(benchmark_curve):
                bm_eq = float(benchmark_curve[i])
                bm_ret = (bm_eq / prev_bm - 1.0) if prev_bm > 0 else 0.0
                prev_bm = bm_eq

            equity_points.append(EquityCurvePoint(
                ts=snap.ts, equity=float(eq),
                period_return=float(period_return), drawdown=float(drawdown),
                benchmark_equity=bm_eq, benchmark_period_return=bm_ret,
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

        A single ``to_dict(orient="index")`` over the whole frame, not
        ``groupby(level="datetime")`` + per-group ``to_dict`` — pandas
        groupby iteration has real per-group construction overhead that
        dominates when there are many small groups (one row per group for
        a single-symbol backtest, which is the common case). Verified
        ~1770x faster on a 97,633-row single-symbol M5 frame (groupby
        ~490 rows/sec vs this ~869k rows/sec), same output.
        """
        result: dict[pd.Timestamp, dict[str, dict[str, float]]] = {}
        raw = self._data.to_dict(orient="index")
        for (sym, ts), row in raw.items():
            result.setdefault(ts, {})[sym] = row
        return result

    @staticmethod
    def _increment_periods_held(positions: dict[str, PositionState]) -> None:
        for ps in positions.values():
            ps.periods_held += 1

    def _eval_equity(
        self,
        cash: float,
        positions: dict[str, PositionState],
        bars: dict[str, dict[str, float]],
    ) -> tuple[float, dict[str, Position]]:
        """Compute portfolio MTM value and position snapshot in a single pass."""
        def _price(sym: str, ps: PositionState) -> float:
            bar = bars.get(sym)
            return bar["close"] if bar is not None else ps.entry_price

        return eval_equity(
            cash, positions,
            get_price=_price,
            get_cost_model=self._get_cost_model,
        )
