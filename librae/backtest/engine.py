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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd

from librae.core import EPSILON

if TYPE_CHECKING:
    from librae.backtest.schema import (
        AllocationSnapshotPoint,
        BacktestOutput,
        EquityCurvePoint,
        OrderEventRecord,
        PositionSnapshotPoint,
        StrategyMetrics,
    )
    from librae.core.run_config import RunConfig

from librae.config.market_config import MarketConfig
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    REASON_FORCE_CLOSE,
    ActionResults,
    OrderEvent,
    TradePnL,
    TradeResult,
    eval_equity,
    liquidate_all,
    merge_pending_intents,
    partition_pending_intent,
    run_pending_and_stops,
    validate_intent_symbols,
    validate_risk_params,
)
from librae.core.strategy import (
    BaseStrategy,
    Context,
    Position,
    PositionState,
    RebalanceTargets,
    StrategyIntent,
)
from librae.core.utils import generate_run_id, infer_timeframe, make_event_id

logger = logging.getLogger(__name__)

_INDEX_NAMES = ["symbol", "datetime"]
_PRICE_COLUMNS = ("open", "high", "low", "close")
_REQUIRED_COLUMNS = (*_PRICE_COLUMNS, "volume")


def _validate_backtest_data(
    data: pd.DataFrame,
    configured_symbols: Sequence[str] | None,
) -> None:
    """Validate the point-in-time OHLCV contract at the engine boundary."""
    if not isinstance(data.index, pd.MultiIndex):
        raise ValueError(
            "data must have MultiIndex (symbol, datetime). "
            "For single-asset: df.index = "
            "pd.MultiIndex.from_arrays([['SYM']*len(df), df.index])"
        )
    if data.index.nlevels != 2 or list(data.index.names) != _INDEX_NAMES:
        raise ValueError("data index levels must be exactly ('symbol', 'datetime')")

    missing = sorted(set(_REQUIRED_COLUMNS) - set(data.columns))
    if missing:
        raise ValueError(f"data missing required OHLCV columns: {', '.join(missing)}")
    if data.empty:
        raise ValueError("data must contain at least one OHLCV bar")
    if not data.index.is_unique:
        raise ValueError("data index must contain unique (symbol, datetime) pairs")

    symbols = data.index.get_level_values("symbol").unique().tolist()
    if any(not isinstance(symbol, str) or not symbol for symbol in symbols):
        raise ValueError("data symbols must be non-empty strings")
    if configured_symbols is not None:
        if len(configured_symbols) != len(set(configured_symbols)):
            raise ValueError("cfg.symbols must not contain duplicates")
        actual = set(symbols)
        expected = set(configured_symbols)
        if actual != expected:
            raise ValueError(
                "cfg.symbols must exactly match data symbols; "
                f"configured={sorted(expected)}, data={sorted(actual)}"
            )

    timestamps = data.index.get_level_values("datetime")
    if not isinstance(timestamps, pd.DatetimeIndex):
        raise ValueError("data datetime index level must contain pandas timestamps")
    if timestamps.tz is None:
        raise ValueError("data datetime index level must be timezone-aware")
    for symbol, symbol_data in data.groupby(level="symbol", sort=False):
        symbol_timestamps = symbol_data.index.get_level_values("datetime")
        if not symbol_timestamps.is_monotonic_increasing:
            raise ValueError(f"data timestamps must be increasing within symbol {symbol!r}")

    non_numeric = [
        column for column in _REQUIRED_COLUMNS if not pd.api.types.is_numeric_dtype(data[column])
    ]
    if non_numeric:
        raise ValueError(f"data OHLCV columns must be numeric: {', '.join(non_numeric)}")

    values = data.loc[:, list(_REQUIRED_COLUMNS)].to_numpy(
        dtype=np.float64,
        na_value=np.nan,
    )
    if not np.isfinite(values).all():
        raise ValueError("data OHLCV values must be finite")

    open_price, high, low, close, volume = values.T
    if np.any(open_price <= 0) or np.any(high <= 0) or np.any(low <= 0) or np.any(close <= 0):
        raise ValueError("data OHLC prices must be positive")
    if np.any(high < np.maximum.reduce([open_price, low, close])) or np.any(
        low > np.minimum.reduce([open_price, high, close])
    ):
        raise ValueError("data OHLC values are inconsistent: low <= open/close <= high is required")
    if np.any(volume < 0):
        raise ValueError("data volume must be non-negative")


@dataclass(frozen=True)
class EquitySnapshot:
    """Single bar equity snapshot."""

    ts: datetime
    equity: float


@dataclass(frozen=True)
class PositionSnapshot:
    """One open position's end-of-bar portfolio snapshot."""

    ts: datetime
    symbol: str
    side: Literal["long", "short"]
    quantity: float
    price: float
    market_value: float
    realized_weight: float


@dataclass(frozen=True)
class AllocationSnapshot:
    """One symbol's target-versus-achieved end-of-event allocation."""

    ts: datetime
    symbol: str
    target_weight: float | None
    realized_weight: float
    weight_drift: float | None


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Portfolio exposure and trading diagnostics for one event."""

    ts: datetime
    gross_exposure: float
    net_exposure: float
    concentration: float
    turnover: float


@dataclass(frozen=True)
class BacktestResult:
    """Raw output from engine — no metrics, just facts."""

    trades: Sequence[TradeResult]
    order_events: Sequence[OrderEvent]
    equity_curve: Sequence[EquitySnapshot]
    position_snapshots: Sequence[PositionSnapshot]
    allocation_snapshots: Sequence[AllocationSnapshot]
    portfolio_snapshots: Sequence[PortfolioSnapshot]
    initial_balance: float
    final_equity: float
    exposed_periods: int = 0


# ---------------------------------------------------------------------------
# Backtest class
# ---------------------------------------------------------------------------


class Backtest:
    """Bar-by-bar backtest engine.

    Two supported construction styles:
      - Direct args (market_config/initial_balance/data_source below): a
        standalone single-run constructor, no RunConfig needed — the
        standard choice for tests, notebooks, and simple scripts.
      - cfg=RunConfig: derives market_config/initial_balance/data_source
        from cfg and additionally resolves a per-symbol CostModel for every
        symbol in a multi-asset run — required when running through the
        CLI/DB pipeline (orchestration/cli.py), which builds a RunConfig.
    Both are first-class; neither is deprecated. Passing market_config
    without cfg only works for spot-multiplier instruments (see the
    ValueError below) — pass cost_model= directly for anything else.

    Args:
        data: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
              For single-asset, wrap with: pd.MultiIndex.from_arrays([["SYM"]*len(df), df.index])
        strategy: BaseStrategy subclass.
        cfg: RunConfig — see "cfg=RunConfig" above.
        market_config: MarketConfig for cost model — direct-args style.
        initial_balance: Starting cash — direct-args style.
        strategy_name: Override strategy name (default: from cfg or snake_case of class name).
        cost_model: CostModel directly (for tests or custom cost models).
        data_source: Data source identifier — direct-args style.
        record_position_snapshots: Record per-symbol end-of-event positions,
            realized weights, and target-versus-achieved allocations. Off by
            default to avoid O(events × configured symbols) memory growth.
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
        record_position_snapshots: bool = False,
    ) -> None:
        _validate_backtest_data(data, cfg.symbols if cfg is not None else None)

        self._data = data
        self._strategy = strategy
        self._cfg = cfg
        self._benchmark_prices: pd.Series | None = None
        self._run_id: str | None = None
        self._timeframe: str | None = None
        self._result: BacktestResult | None = None
        self._metrics: StrategyMetrics | None = None
        self._record_position_snapshots = record_position_snapshots

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
                    "the symbol registry (spot=1.0 auto, contract_* explicit-required), which "
                    "the direct-args constructor has no symbol for. Pass cost_model= directly "
                    "(e.g. CostModel.from_market(market_config, multiplier=...)), or use cfg= instead."
                )
            else:
                resolved_cm = CostModel.zero()

        self._cost_models: dict[str, CostModel] = {"__default__": resolved_cm}
        if cfg is not None and cost_model is None:
            # Resolve every other symbol in this run independently (each
            # against its own registry entry/cost_overrides/symbol_overrides
            # entry) — a multi-asset run isn't guaranteed to share one
            # multiplier (e.g. tw_futures TXFR1=200 vs MXFR1=50), so only
            # cfg.symbol (symbols[0]) got a correct CostModel above.
            for sym in self._symbols:
                if sym != cfg.symbol:
                    self._cost_models[sym] = CostModel.from_config(cfg, symbol=sym)
        self._fill_price: str = (cfg.params or {}).get("fill_price", "open") if cfg else "open"
        self._risk_limits = validate_risk_params(cfg.params if cfg else None)

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
        """Get a symbol override or the constructor-created default model."""
        return self._cost_models.get(symbol, self._cost_models["__default__"])

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
        universe = set(self._symbols)
        pending_intent: StrategyIntent = []
        position_snapshots: list[PositionSnapshot] = []
        allocation_snapshots: list[AllocationSnapshot] = []
        portfolio_snapshots: list[PortfolioSnapshot] = []
        active_target_weights: dict[str, float] | None = None
        last_prices: dict[str, float] = {}
        decision_index = 0
        equity_peak = self._initial_balance
        last_equity = self._initial_balance
        halted = False

        for ts in self._timeline:
            event_start_index = len(all_events)
            bars = all_bars[ts]
            for symbol, bar in bars.items():
                close = bar.get("close")
                if close is not None and np.isfinite(close) and close > 0:
                    last_prices[symbol] = float(close)

            intent_to_execute, pending_intent = partition_pending_intent(
                pending_intent,
                bars,
                positions,
                primary_symbol=primary_symbol,
            )
            if isinstance(intent_to_execute, RebalanceTargets):
                active_target_weights = dict(intent_to_execute.weights)

            # ── Steps 1+1.5: fill previous bar's pending actions at current
            # bar's price, then check stop-loss/take-profit — shared with
            # LiveTrader's simulation mode so deterministic runtimes cannot
            # drift on this sequence ──
            max_position_notional = (
                self._risk_limits.max_position_pct * last_equity
                if self._risk_limits.max_position_pct
                else None
            )
            if halted:
                step_result = ActionResults(trades=[], events=[], cash_delta=0.0)
            else:
                cash, step_result = run_pending_and_stops(
                    ts,
                    positions,
                    cash,
                    intent_to_execute,
                    bars,
                    get_cost_model=self._get_cost_model,
                    default_fill=self._fill_price,
                    primary_symbol=primary_symbol,
                    max_position_notional=max_position_notional,
                    max_volume_participation_pct=(self._risk_limits.max_volume_participation_pct),
                    max_gross_exposure_pct=self._risk_limits.max_gross_exposure_pct,
                    max_net_exposure_pct=self._risk_limits.max_net_exposure_pct,
                )
            trades.extend(step_result.trades)
            all_events.extend(step_result.events)
            if halted and positions:
                liquidation_result = liquidate_all(
                    positions,
                    bars,
                    ts,
                    get_cost_model=self._get_cost_model,
                    reason=REASON_DRAWDOWN_BREACH,
                    max_volume_participation_pct=(self._risk_limits.max_volume_participation_pct),
                )
                trades.extend(liquidation_result.trades)
                all_events.extend(liquidation_result.events)
                cash += liquidation_result.cash_delta

            # ── Step 2: equity and drawdown check ──
            mtm, pos_snapshot = self._eval_equity(cash, positions, last_prices)
            had_exposure = bool(positions)
            if had_exposure:
                exposed_periods += 1

            equity_peak = max(equity_peak, mtm)
            drawdown = (mtm - equity_peak) / equity_peak if equity_peak > 0 else 0.0
            if (
                self._risk_limits.max_drawdown_pct
                and not halted
                and equity_peak > 0
                and drawdown <= -self._risk_limits.max_drawdown_pct
            ):
                dd_result = liquidate_all(
                    positions,
                    bars,
                    ts,
                    get_cost_model=self._get_cost_model,
                    reason=REASON_DRAWDOWN_BREACH,
                    max_volume_participation_pct=(self._risk_limits.max_volume_participation_pct),
                    used_volume=self._filled_quantities(all_events[event_start_index:]),
                )
                trades.extend(dd_result.trades)
                all_events.extend(dd_result.events)
                cash += dd_result.cash_delta
                halted = True
                # The liquidation is an event at this timestamp, so its costs
                # and resulting flat position must be reflected in the same
                # end-of-bar snapshot.
                mtm, pos_snapshot = self._eval_equity(cash, positions, last_prices)
                logger.warning(
                    "Backtest halted at %s: drawdown %.2f%% breached max_drawdown_pct=%.2f%% "
                    "— liquidation started",
                    ts,
                    drawdown * 100,
                    self._risk_limits.max_drawdown_pct * 100,
                )

            equity_curve.append(EquitySnapshot(ts=ts, equity=mtm))
            event_turnover = (
                sum(event.notional for event in all_events[event_start_index:]) / mtm
                if mtm > EPSILON
                else 0.0
            )
            portfolio_snapshots.append(
                self._snapshot_portfolio(
                    ts,
                    positions,
                    last_prices,
                    mtm,
                    event_turnover,
                )
            )
            if self._record_position_snapshots:
                position_snapshots.extend(self._snapshot_positions(ts, positions, last_prices, mtm))
                allocation_snapshots.extend(
                    self._snapshot_allocations(
                        ts,
                        positions,
                        last_prices,
                        mtm,
                        active_target_weights,
                    )
                )

            last_equity = mtm

            # ── Step 3: strategy decision (produces next bar's pending actions) ──
            if halted:
                pending_intent = []
            elif not isinstance(pending_intent, RebalanceTargets):
                ctx = Context(
                    ts=ts,
                    symbol=primary_symbol,
                    symbols=self._symbols,
                    bar=bars.get(primary_symbol, {}),
                    bars=bars,
                    positions=pos_snapshot,
                    cash=cash,
                    equity=mtm,
                    period_index=decision_index,
                )
                new_intent = self._strategy.on_bar(ctx)
                validate_intent_symbols(
                    new_intent,
                    universe,
                    primary_symbol=primary_symbol,
                )
                pending_intent = merge_pending_intents(
                    pending_intent,
                    new_intent,
                    primary_symbol=primary_symbol,
                )
                decision_index += 1

            self._increment_periods_held(positions, bars)

        # WHY: the final intent is discarded because there is no T+1 bar to fill it.
        if pending_intent:
            logger.warning("Discarding unresolved end-of-run strategy intent: %r", pending_intent)
        # Force-close all open positions at last bar
        if self._timeline:
            last_ts = self._timeline[-1]
            last_bars = all_bars[last_ts]
            close_result = liquidate_all(
                positions,
                last_bars,
                last_ts,
                get_cost_model=self._get_cost_model,
                reason=REASON_FORCE_CLOSE,
                max_volume_participation_pct=self._risk_limits.max_volume_participation_pct,
                used_volume=self._filled_quantities(
                    event for event in all_events if event.ts == last_ts
                ),
            )
            if positions:
                raise ValueError(
                    "cannot force-close all positions at the backtest end under "
                    "available price/volume constraints: "
                    f"{sorted(positions)}"
                )
            trades.extend(close_result.trades)
            all_events.extend(close_result.events)
            cash += close_result.cash_delta
            # WHY: forced liquidation happens after the bar snapshot. Replace
            # that point so the curve, metrics, and final account cash reconcile
            # without creating a duplicate timestamp.
            if equity_curve:
                equity_curve[-1] = EquitySnapshot(ts=last_ts, equity=cash)
            if self._record_position_snapshots:
                position_snapshots = [
                    snapshot for snapshot in position_snapshots if snapshot.ts != last_ts
                ]
                allocation_snapshots = [
                    snapshot for snapshot in allocation_snapshots if snapshot.ts != last_ts
                ]
                allocation_snapshots.extend(
                    self._snapshot_allocations(
                        last_ts,
                        positions,
                        last_prices,
                        cash,
                        active_target_weights,
                    )
                )
            portfolio_snapshots[-1] = self._snapshot_portfolio(
                last_ts,
                positions,
                last_prices,
                cash,
                sum(event.notional for event in all_events if event.ts == last_ts) / cash
                if cash > EPSILON
                else 0.0,
            )

        self._result = BacktestResult(
            trades=trades,
            order_events=all_events,
            equity_curve=equity_curve,
            initial_balance=self._initial_balance,
            final_equity=cash,
            exposed_periods=exposed_periods,
            position_snapshots=position_snapshots,
            allocation_snapshots=allocation_snapshots,
            portfolio_snapshots=portfolio_snapshots,
        )
        return self._result

    def build_output(
        self,
        *,
        annualize: bool | None = None,
    ) -> BacktestOutput:
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
            BacktestOutput,
            RunMetadata,
        )
        from librae.core.metrics import compute_all

        if self._result is None:
            raise RuntimeError("Call run() before build_output()")

        result = self._result
        run_id = self._run_id

        timeline = self._timeline
        started_at = (
            timeline[0].to_pydatetime() if hasattr(timeline[0], "to_pydatetime") else timeline[0]
        )
        ended_at = (
            timeline[-1].to_pydatetime() if hasattr(timeline[-1], "to_pydatetime") else timeline[-1]
        )
        symbol = self._symbols[0]
        timeframe = self._timeframe

        # Benchmark — computed here, not in run() (analysis config, not trade facts)
        benchmark_curve = self._compute_benchmark()

        # Build TradePnL list + periods_held from TradeResult
        trade_pnl_list = [
            TradePnL(
                gross_pnl=t.gross_pnl,
                net_pnl=t.net_pnl,
                commission=t.commission,
                slippage=t.slippage,
                tax=t.tax,
                gross_return=t.gross_return,
                net_return=t.net_return,
                exit_commission=0.0,
                exit_slippage=0.0,
                exit_tax=t.tax,
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
            turnover_values=[snapshot.turnover for snapshot in result.portfolio_snapshots],
            gross_exposure_values=[
                snapshot.gross_exposure for snapshot in result.portfolio_snapshots
            ],
            net_exposure_values=[snapshot.net_exposure for snapshot in result.portfolio_snapshots],
            concentration_values=[
                snapshot.concentration for snapshot in result.portfolio_snapshots
            ],
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
            run_at=datetime.now(tz=UTC),
        )

        event_records = self._build_event_records(result, run_id)
        equity_points = self._enrich_equity_curve(result, benchmark_curve)
        position_snapshot_points = self._build_position_snapshot_records(result)
        allocation_snapshot_points = self._build_allocation_snapshot_records(result)

        return BacktestOutput(
            run_metadata=run_metadata,
            equity_curve=tuple(equity_points),
            order_events=tuple(event_records),
            metrics=self._metrics,
            position_snapshots=tuple(position_snapshot_points),
            allocation_snapshots=tuple(allocation_snapshot_points),
        )

    @property
    def metrics(self) -> StrategyMetrics:
        """Access StrategyMetrics. Raises RuntimeError if build_output not called."""
        if self._metrics is None:
            raise RuntimeError("Call build_output() before accessing metrics")
        return self._metrics

    @staticmethod
    def _build_event_records(result: BacktestResult, run_id: str) -> list[OrderEventRecord]:
        """Map OrderEvent -> OrderEventRecord."""
        from librae.backtest.schema import OrderEventRecord

        return [
            OrderEventRecord(
                event_id=make_event_id(run_id, i),
                ts=e.ts,
                symbol=e.symbol,
                side=e.side,
                event_type=e.event_type,
                fill_quantity=float(e.fill_quantity),
                price=float(e.price),
                entry_price=float(e.entry_price),
                remaining_quantity=float(e.remaining_quantity),
                notional=float(e.notional),
                commission=float(e.commission),
                slippage=float(e.slippage),
                tax=float(e.tax),
                pnl=float(e.pnl) if e.pnl is not None else None,
                net_return=float(e.net_return) if e.net_return is not None else None,
                entry_at=e.entry_at,
                periods_held=e.periods_held,
                reason=e.reason,
            )
            for i, e in enumerate(result.order_events)
        ]

    @staticmethod
    def _build_position_snapshot_records(
        result: BacktestResult,
    ) -> list[PositionSnapshotPoint]:
        """Map raw engine position snapshots to the canonical output schema."""
        from librae.backtest.schema import PositionSnapshotPoint

        return [
            PositionSnapshotPoint(
                ts=snapshot.ts,
                symbol=snapshot.symbol,
                side=snapshot.side,
                quantity=float(snapshot.quantity),
                price=float(snapshot.price),
                market_value=float(snapshot.market_value),
                realized_weight=float(snapshot.realized_weight),
            )
            for snapshot in result.position_snapshots
        ]

    @staticmethod
    def _build_allocation_snapshot_records(
        result: BacktestResult,
    ) -> list[AllocationSnapshotPoint]:
        """Map target-versus-achieved allocation facts to output schema."""
        from librae.backtest.schema import AllocationSnapshotPoint

        return [
            AllocationSnapshotPoint(
                ts=snapshot.ts,
                symbol=snapshot.symbol,
                target_weight=snapshot.target_weight,
                realized_weight=float(snapshot.realized_weight),
                weight_drift=snapshot.weight_drift,
            )
            for snapshot in result.allocation_snapshots
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
            portfolio = result.portfolio_snapshots[i]
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

            equity_points.append(
                EquityCurvePoint(
                    ts=snap.ts,
                    equity=float(eq),
                    period_return=float(period_return),
                    drawdown=float(drawdown),
                    benchmark_equity=bm_eq,
                    benchmark_period_return=bm_ret,
                    gross_exposure=float(portfolio.gross_exposure),
                    net_exposure=float(portfolio.net_exposure),
                    concentration=float(portfolio.concentration),
                    turnover=float(portfolio.turnover),
                )
            )
        return equity_points

    def _compute_benchmark(self) -> list[float] | None:
        """Compute a timestamp-aligned benchmark buy-and-hold equity curve."""
        if self._benchmark_prices is None:
            return None
        prices = self._benchmark_prices
        if prices.empty:
            return None
        if not isinstance(prices.index, pd.DatetimeIndex):
            raise ValueError("benchmark prices must have a DatetimeIndex")
        if not prices.index.is_unique:
            raise ValueError("benchmark prices must have a unique DatetimeIndex")

        timeline = pd.DatetimeIndex(self._timeline)
        if prices.index.tz != timeline.tz:
            raise ValueError("benchmark timezone must match the backtest timeline")

        aligned = prices.astype("float64").sort_index().reindex(timeline, method="ffill")
        if aligned.isna().any():
            raise ValueError("benchmark must contain a price at or before backtest start")
        if not np.isfinite(aligned.to_numpy()).all() or (aligned <= 0).any():
            raise ValueError("benchmark prices must be finite and positive")

        initial_price = float(aligned.iloc[0])
        return (self._initial_balance * aligned / initial_price).tolist()

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
    def _increment_periods_held(
        positions: dict[str, PositionState],
        bars: dict[str, dict[str, float]],
    ) -> None:
        """Advance holding age only when that position has a market bar."""
        for symbol, position in positions.items():
            if symbol in bars:
                position.periods_held += 1

    def _eval_equity(
        self,
        cash: float,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
    ) -> tuple[float, dict[str, Position]]:
        """Compute portfolio MTM from the latest point-in-time marks."""

        def _price(sym: str, _position: PositionState) -> float:
            try:
                return last_prices[sym]
            except KeyError as exc:
                raise RuntimeError(
                    f"no point-in-time mark available for open position {sym}"
                ) from exc

        return eval_equity(
            cash,
            positions,
            get_price=_price,
            get_cost_model=self._get_cost_model,
        )

    def _snapshot_positions(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
    ) -> list[PositionSnapshot]:
        """Build deterministic end-of-bar position and realized-weight facts."""
        snapshots: list[PositionSnapshot] = []
        for symbol in sorted(positions):
            position = positions[symbol]
            price = last_prices[symbol]
            signed_market_value = (
                price
                * position.quantity
                * self._get_cost_model(symbol).multiplier
                * (-1.0 if position.side == "short" else 1.0)
            )
            realized_weight = signed_market_value / equity if abs(equity) > EPSILON else 0.0
            snapshots.append(
                PositionSnapshot(
                    ts=ts,
                    symbol=symbol,
                    side=position.side,
                    quantity=position.quantity,
                    price=price,
                    market_value=signed_market_value,
                    realized_weight=realized_weight,
                )
            )
        return snapshots

    def _realized_weights(
        self,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
    ) -> dict[str, float]:
        if equity <= EPSILON:
            return {symbol: 0.0 for symbol in positions}
        return {
            symbol: (
                last_prices[symbol]
                * position.quantity
                * self._get_cost_model(symbol).multiplier
                * (-1.0 if position.side == "short" else 1.0)
                / equity
            )
            for symbol, position in positions.items()
        }

    def _snapshot_allocations(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
        target_weights: dict[str, float] | None,
    ) -> list[AllocationSnapshot]:
        """Record every configured symbol, including unfilled target names."""
        realized_weights = self._realized_weights(positions, last_prices, equity)
        snapshots = []
        for symbol in sorted(self._symbols):
            target_weight = target_weights.get(symbol, 0.0) if target_weights is not None else None
            realized_weight = realized_weights.get(symbol, 0.0)
            snapshots.append(
                AllocationSnapshot(
                    ts=ts,
                    symbol=symbol,
                    target_weight=target_weight,
                    realized_weight=realized_weight,
                    weight_drift=(
                        realized_weight - target_weight if target_weight is not None else None
                    ),
                )
            )
        return snapshots

    def _snapshot_portfolio(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
        turnover: float,
    ) -> PortfolioSnapshot:
        """Compute end-of-event exposure ratios from signed market values."""
        realized_weights = self._realized_weights(positions, last_prices, equity)
        return PortfolioSnapshot(
            ts=ts,
            gross_exposure=sum(abs(weight) for weight in realized_weights.values()),
            net_exposure=sum(realized_weights.values()),
            concentration=max((abs(weight) for weight in realized_weights.values()), default=0.0),
            turnover=turnover,
        )

    @staticmethod
    def _filled_quantities(events: Iterable[OrderEvent]) -> dict[str, float]:
        """Aggregate quantity already matched per symbol in one data event."""
        quantities: dict[str, float] = {}
        for event in events:
            quantities[event.symbol] = quantities.get(event.symbol, 0.0) + event.fill_quantity
        return quantities
