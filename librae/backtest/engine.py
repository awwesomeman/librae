"""Backtest engine — bar-by-bar execution with Strategy + Executor pattern.

Usage:
    from librae.core.run_config import RunConfig

    bt = Backtest(data=df, strategy=my_strategy, config=config)
    bt.add_benchmark(df.xs("BTCUSDT", level="symbol")["close"])
    bt.run()
    output = bt.build_output()

The engine owns all position state. Strategies only observe via Context.
Execution uses deterministic simulation functions from core.executor.
Shared PnL calculation uses calc_trade_pnl() from core.executor.

Data format: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
Single-asset is a special case where symbols has one element.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from numbers import Real
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from librae.backtest.result import (
    AccountBacktestResult,
    AllocationSnapshot,
    BacktestResult,
    EquitySnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
)
from librae.core import EPSILON

if TYPE_CHECKING:
    from librae.backtest.schema import (
        AllocationSnapshotPoint,
        BacktestOutput,
        EquityCurvePoint,
        FundingCashFlowRecord,
        OrderEventRecord,
        PositionSnapshotPoint,
        StrategyMetrics,
    )
    from librae.core.run_config import RunConfig

from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    REASON_FORCE_CLOSE,
    ExecutionResult,
    OrderEvent,
    TradePnL,
    TradeResult,
    calc_equity,
    calculate_position_weights,
    calculate_signed_position_notionals,
    check_stop_targets,
    execute_pending_decision_and_stops,
    liquidate_all,
    merge_pending_decisions,
    partition_pending_decision,
    queue_market_exit_all,
    validate_strategy_decision,
)
from librae.core.funding import FundingCashFlow, calculate_funding_cash_flows
from librae.core.liquidity import calculate_lagged_adv
from librae.core.market_data import validate_ohlcv_values
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import (
    AccountSnapshot,
    Context,
    MultiLegOrder,
    PortfolioTargets,
    Position,
    PositionState,
    Strategy,
    StrategyDecision,
)
from librae.core.trading_calendar import session_labels, validate_calendar_id
from librae.core.utils import (
    generate_run_id,
    infer_timeframe,
    interval_to_timedelta,
    make_event_id,
    to_canonical,
)

logger = logging.getLogger(__name__)

_INDEX_NAMES = ["symbol", "datetime"]


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

    if not data.index.is_unique:
        raise ValueError("data index must contain unique (symbol, datetime) pairs")

    symbols = data.index.get_level_values("symbol").unique().tolist()
    if any(not isinstance(symbol, str) or not symbol for symbol in symbols):
        raise ValueError("data symbols must be non-empty strings")
    if configured_symbols is not None:
        if len(configured_symbols) != len(set(configured_symbols)):
            raise ValueError("config.symbols must not contain duplicates")
        actual = set(symbols)
        expected = set(configured_symbols)
        if actual != expected:
            raise ValueError(
                "config.symbols must exactly match data symbols; "
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

    validate_ohlcv_values(data)


def _resolve_data_timeframe(data: pd.DataFrame, configured_timeframe: str | None) -> str:
    """Infer each symbol independently and require one coherent bar interval."""
    indexes_by_symbol = {
        str(symbol): pd.DatetimeIndex(symbol_data.index.get_level_values("datetime"))
        for symbol, symbol_data in data.groupby(level="symbol", sort=False)
    }
    inferred_by_symbol = {
        symbol: infer_timeframe(index[:20])
        for symbol, index in indexes_by_symbol.items()
        if len(index) >= 5
    }
    if configured_timeframe is not None:
        expected = to_canonical(configured_timeframe)
        mismatches = {
            symbol: timeframe
            for symbol, timeframe in inferred_by_symbol.items()
            if timeframe != expected
        }
        if mismatches:
            raise ValueError(
                f"config.timeframe={expected} does not match per-symbol data "
                f"timeframes={mismatches}"
            )
        data_timeframe = expected
    else:
        if not inferred_by_symbol:
            raise ValueError(
                "cannot infer a data timeframe: at least one symbol requires five bars"
            )
        inferred = set(inferred_by_symbol.values())
        if len(inferred) != 1:
            raise ValueError(f"data symbols have inconsistent timeframes: {inferred_by_symbol}")
        data_timeframe = next(iter(inferred))

    if data_timeframe.startswith("MN"):
        month_interval = int(data_timeframe[2:])
        for symbol, index in indexes_by_symbol.items():
            if len(index) < 2:
                continue
            month_ordinals = index.tz_localize(None).to_period("M").asi8
            month_diffs = np.diff(month_ordinals)
            if np.any(month_diffs < month_interval) or np.any(month_diffs % month_interval != 0):
                raise ValueError(
                    f"data symbol {symbol!r} timestamps are not aligned to "
                    f"timeframe={data_timeframe}"
                )
        return data_timeframe

    base_interval = interval_to_timedelta(data_timeframe)
    for symbol, index in indexes_by_symbol.items():
        if len(index) < 2:
            continue
        diffs = pd.Series(index).diff().dropna()
        if any(diff < base_interval or diff % base_interval != pd.Timedelta(0) for diff in diffs):
            raise ValueError(
                f"data symbol {symbol!r} timestamps are not aligned to timeframe={data_timeframe}"
            )
    return data_timeframe


# ---------------------------------------------------------------------------
# Backtest class
# ---------------------------------------------------------------------------


class Backtest:
    """Bar-by-bar backtest engine.

    Two supported construction styles:
      - Direct args (initial_balance/data_source below): a
        standalone single-run constructor, no RunConfig needed — the
        standard choice for tests, notebooks, and simple scripts.
      - config=RunConfig: derives cost model/initial_balance/data_source
        from config and additionally resolves a per-symbol CostModel for every
        symbol in a multi-asset run — required when running through the
        CLI/DB pipeline (orchestration/cli.py), which builds a RunConfig.
    Both are first-class; neither is deprecated.

    Args:
        data: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
              For single-asset, wrap with: pd.MultiIndex.from_arrays([["SYM"]*len(df), df.index])
        strategy: Strategy subclass.
        config: RunConfig — see "config=RunConfig" above.
        initial_balance: Starting cash — direct-args style.
        strategy_name: Override strategy name (default: from config or snake_case of class name).
        cost_model: CostModel directly (for tests or custom cost models).
        data_source: Data source identifier — direct-args style.
        record_position_snapshots: Record per-symbol end-of-event positions,
            realized weights, and target-versus-achieved allocations. Off by
            default to avoid O(events × configured symbols) memory growth.
        execution: Fill-price and volume assumptions for direct
            construction. With ``config``, ``config.execution`` is the only source.
        risk: Portfolio risk limits for direct construction. With ``config``,
            ``config.risk`` is the only source.
    """

    def __init__(
        self,
        data: pd.DataFrame,
        strategy: Strategy,
        config: RunConfig | None = None,
        initial_balance: float = 100_000.0,
        *,
        account_id: str = "default",
        currency: str = "USD",
        strategy_name: str | None = None,
        cost_model: CostModel | None = None,
        data_source: str = "",
        record_position_snapshots: bool = False,
        execution: ExecutionPolicy | None = None,
        risk: RiskPolicy | None = None,
    ) -> None:
        _validate_backtest_data(data, config.symbols if config is not None else None)
        if config is not None and execution is not None:
            raise ValueError(
                "execution cannot override config.execution; use one configuration source"
            )
        if config is not None and risk is not None:
            raise ValueError("risk cannot override config.risk; use one configuration source")
        if (
            isinstance(initial_balance, bool)
            or not isinstance(initial_balance, Real)
            or not np.isfinite(initial_balance)
            or initial_balance <= 0
        ):
            raise ValueError("initial_balance must be finite and positive")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id must be a non-empty string")
        if not isinstance(currency, str) or not currency:
            raise ValueError("currency must be a non-empty string")

        self._data = data
        self._strategy = strategy
        self._config = config
        self._benchmark_prices: pd.Series | None = None
        self._run_id: str | None = None
        self._timeframe: str | None = None
        self._result: BacktestResult | None = None
        self._metrics: StrategyMetrics | None = None
        self._record_position_snapshots = record_position_snapshots

        self._symbols = (
            list(config.symbols)
            if config is not None
            else data.index.get_level_values(0).unique().tolist()
        )
        self._timeline = sorted(data.index.get_level_values("datetime").unique())
        from librae.config.symbols import load_symbol_registry, resolve_symbol

        registry = load_symbol_registry()
        instrument_overrides = config.instrument_overrides if config is not None else {}
        self._calendar_ids = {
            symbol: (instrument_overrides or {}).get(symbol, {}).get("calendar_id")
            or (registry[symbol].calendar_id if symbol in registry else None)
            for symbol in self._symbols
        }

        # Resolve from config or explicit args
        if config is not None:
            for symbol in self._symbols:
                resolve_symbol(config, symbol)
            self._account_id = config.account_id
            self._currency = config.account.currency
            self._initial_cash = config.account.initial_cash
            self._data_source = config.data_source
            resolved_name = config.strategy_name
            resolved_cm = CostModel.from_config(config, override=cost_model)
        else:
            self._account_id = account_id
            self._currency = currency
            self._initial_cash = initial_balance
            self._data_source = data_source
            resolved_name = None
            resolved_cm = cost_model if cost_model is not None else CostModel.zero()

        self._cost_models: dict[str, CostModel] = {"__default__": resolved_cm}
        if config is not None and cost_model is None:
            # Resolve every other symbol in this run independently (each
            # against its own registry entry/cost_overrides/symbol_cost_overrides
            # entry) — a multi-asset run isn't guaranteed to share one
            # multiplier (e.g. tw_futures TXFR1=200 vs MXFR1=50), so only
            # config.symbol (symbols[0]) got a correct CostModel above.
            for sym in self._symbols:
                if sym != config.symbol:
                    self._cost_models[sym] = CostModel.from_config(config, symbol=sym)
        resolved_execution = config.execution if config else execution or ExecutionPolicy()
        self._fill_price = resolved_execution.default_fill_price
        self._max_bar_volume_participation_rate = (
            resolved_execution.max_bar_volume_participation_rate
        )
        self._adv_lookback_sessions = resolved_execution.adv_lookback_sessions
        self._max_adv_participation_rate = resolved_execution.max_adv_participation_rate
        self._risk_policy = config.risk if config else risk or RiskPolicy()

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

    @staticmethod
    def _without_halted_account(
        decision: StrategyDecision,
        halted: bool,
    ) -> StrategyDecision:
        """Discard exposure decisions after the account is drawdown-halted."""
        return [] if halted else decision

    def _execute_steps(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        cash: float,
        decision: StrategyDecision,
        bars: dict[str, dict[str, float]],
        *,
        primary_symbol: str,
        last_equity: float,
        halted: bool,
        get_lagged_adv: Callable[[str], float | None],
        used_adv_quantity_by_symbol: dict[str, float],
        exposure_prices: dict[str, float],
    ) -> tuple[float, ExecutionResult]:
        if halted:
            result = check_stop_targets(
                positions,
                bars,
                ts,
                get_cost_model=self._get_cost_model,
                max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
                max_adv_participation_rate=self._max_adv_participation_rate,
                get_lagged_adv=get_lagged_adv,
                used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
            )
            return cash + result.cash_delta, result
        max_position_notional = (
            self._risk_policy.max_position_weight * last_equity
            if self._risk_policy.max_position_weight
            else None
        )
        return execute_pending_decision_and_stops(
            ts,
            positions,
            cash,
            decision,
            bars,
            get_cost_model=self._get_cost_model,
            default_fill=self._fill_price,
            primary_symbol=primary_symbol,
            max_position_notional=max_position_notional,
            max_order_notional=self._risk_policy.max_order_notional,
            max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
            max_adv_participation_rate=self._max_adv_participation_rate,
            get_lagged_adv=get_lagged_adv,
            used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
            max_gross_exposure=self._risk_policy.max_gross_exposure,
            max_net_exposure=self._risk_policy.max_net_exposure,
            exposure_prices=exposure_prices,
        )

    # --- Private helpers ---

    def _get_cost_model(self, symbol: str) -> CostModel:
        """Get a symbol override or the constructor-created default model."""
        return self._cost_models.get(symbol, self._cost_models["__default__"])

    def run(self) -> BacktestResult:
        """Execute the backtest. Generates run_id at start. Returns BacktestResult."""
        self._timeframe = _resolve_data_timeframe(
            self._data,
            self._config.timeframe if self._config is not None else None,
        )
        if self._adv_lookback_sessions is not None and self._timeframe != "D1":
            missing_calendars = sorted(
                symbol for symbol, calendar_id in self._calendar_ids.items() if calendar_id is None
            )
            if missing_calendars:
                raise ValueError(
                    "intraday ADV requires calendar_id for every symbol; missing "
                    f"{missing_calendars}"
                )
            for calendar_id in self._calendar_ids.values():
                validate_calendar_id(calendar_id)
        self._run_id = generate_run_id(self._strategy_name, self._symbols[0], self._timeframe)
        logger.info("Backtest started: run_id=%s", self._run_id)

        # WHY: pre-convert all bars once — avoids per-bar DataFrame.to_dict()
        # which is the dominant cost in the hot loop.
        all_bars = self._precompute_bars()
        all_lagged_adv = self._precompute_lagged_adv()
        all_session_labels = self._precompute_session_labels()

        cash = self._initial_cash
        positions: dict[str, PositionState] = {}
        trades: list[TradeResult] = []
        all_events: list[OrderEvent] = []
        equity_curve: list[EquitySnapshot] = []
        exposed_periods = 0
        primary_symbol = self._symbols[0]
        universe = set(self._symbols)
        pending_decision: StrategyDecision = []
        position_snapshots: list[PositionSnapshot] = []
        allocation_snapshots: list[AllocationSnapshot] = []
        funding_cash_flows: list[FundingCashFlow] = []
        portfolio_snapshots: list[PortfolioSnapshot] = []
        active_target_weights: dict[str, float] | None = None
        last_prices: dict[str, float] = {}
        decision_index = 0
        equity_peak = self._initial_cash
        last_equity = self._initial_cash
        halted = False
        adv_session_by_symbol: dict[str, object] = {}
        used_adv_quantity_by_symbol: dict[str, float] = {}

        for ts in self._timeline:
            event_start_index = len(all_events)
            bars = all_bars[ts]
            lagged_adv = all_lagged_adv.get(ts, {})
            current_session_labels = all_session_labels.get(ts, {})
            for symbol, label in current_session_labels.items():
                if adv_session_by_symbol.get(symbol) != label:
                    adv_session_by_symbol[symbol] = label
                    used_adv_quantity_by_symbol[symbol] = 0.0

            def get_lagged_adv(
                symbol: str,
                values: dict[str, float] = lagged_adv,
            ) -> float | None:
                return values.get(symbol)

            exposure_prices = dict(last_prices)
            for symbol, bar in bars.items():
                open_price = bar.get("open")
                if open_price is not None and np.isfinite(open_price) and open_price > 0:
                    exposure_prices[symbol] = float(open_price)
                close = bar.get("close")
                if close is not None and np.isfinite(close) and close > 0:
                    last_prices[symbol] = float(close)

            pending_decision = self._without_halted_account(pending_decision, halted)
            decision_to_execute, pending_decision = partition_pending_decision(
                pending_decision,
                bars,
                positions,
                primary_symbol=primary_symbol,
            )
            if isinstance(decision_to_execute, PortfolioTargets):
                active_target_weights = dict(decision_to_execute.weights)

            # ── Steps 1+1.5: fill the previous pending decision at current
            # bar's price, then check stop-loss/take-profit — shared with
            # LiveTrader's simulation mode so deterministic runtimes cannot
            # drift on this sequence ──
            cash, step_result = self._execute_steps(
                ts,
                positions,
                cash,
                decision_to_execute,
                bars,
                primary_symbol=primary_symbol,
                last_equity=last_equity,
                halted=halted,
                get_lagged_adv=get_lagged_adv,
                used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
                exposure_prices=exposure_prices,
            )
            trades.extend(step_result.trades)
            all_events.extend(step_result.events)
            _, event_funding_cash_flows = calculate_funding_cash_flows(
                ts,
                bars,
                positions,
                get_cost_model=self._get_cost_model,
            )
            for cash_flow in event_funding_cash_flows:
                cash += cash_flow.cash_flow
            funding_cash_flows.extend(event_funding_cash_flows)
            # ── Step 2: equity and drawdown check ──
            mtm, position_view = self._calc_equity_snapshot(
                cash,
                positions,
                last_prices,
            )
            if positions:
                exposed_periods += 1
            equity_peak = max(equity_peak, mtm)
            drawdown = (mtm - equity_peak) / equity_peak if equity_peak > 0 else 0.0
            breached = bool(
                self._risk_policy.max_drawdown_rate
                and not halted
                and drawdown <= -self._risk_policy.max_drawdown_rate
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
                position_snapshots.extend(
                    self._snapshot_positions(
                        ts,
                        positions,
                        last_prices,
                        mtm,
                    )
                )
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
            account_snapshot = AccountSnapshot(
                currency=self._currency,
                cash=cash,
                equity=mtm,
            )

            if breached:
                queue_market_exit_all(
                    positions,
                    reason=REASON_DRAWDOWN_BREACH,
                )
                halted = True
                logger.warning(
                    "Backtest account %s halted at %s: drawdown %.2f%% breached "
                    "max_drawdown_rate=%.2f%% — market exits queued for next "
                    "observed opens",
                    self._account_id,
                    ts,
                    drawdown * 100,
                    self._risk_policy.max_drawdown_rate * 100,
                )

            # ── Step 3: strategy decision (becomes eligible on a later bar) ──
            if halted:
                pending_decision = []
            elif not isinstance(pending_decision, (PortfolioTargets, MultiLegOrder)):
                ctx = Context(
                    ts=ts,
                    symbol=primary_symbol,
                    symbols=self._symbols,
                    bar=bars.get(primary_symbol, {}),
                    bars=bars,
                    positions=position_view,
                    account_id=self._account_id,
                    account=account_snapshot,
                    period_index=decision_index,
                )
                new_decision = self._strategy.on_bar(ctx)
                validate_strategy_decision(
                    new_decision,
                    universe,
                    primary_symbol=primary_symbol,
                )
                new_decision = self._without_halted_account(new_decision, halted)
                pending_decision = merge_pending_decisions(
                    pending_decision,
                    new_decision,
                    primary_symbol=primary_symbol,
                )
                decision_index += 1

            self._increment_periods_held(positions, bars)

        # WHY: the final intent is discarded because there is no T+1 bar to fill it.
        if pending_decision:
            logger.warning(
                "Discarding unresolved end-of-run strategy decision: %r", pending_decision
            )
        unresolved_risk_exits = sorted(
            symbol
            for symbol, position in positions.items()
            if position.pending_market_exit_reason is not None
        )
        if unresolved_risk_exits:
            raise ValueError(
                "cannot execute queued risk exits without a subsequent tradable bar: "
                f"{unresolved_risk_exits}"
            )
        # Force-close all open positions at last bar
        if self._timeline:
            last_ts = self._timeline[-1]
            last_bars = all_bars[last_ts]
            used_bar_quantity = self._filled_quantities(
                event for event in all_events if event.ts == last_ts
            )
            close_result = liquidate_all(
                positions,
                last_bars,
                last_ts,
                get_cost_model=self._get_cost_model,
                reason=REASON_FORCE_CLOSE,
                max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
                max_adv_participation_rate=self._max_adv_participation_rate,
                get_lagged_adv=lambda symbol: all_lagged_adv.get(last_ts, {}).get(symbol),
                used_bar_quantity_by_symbol=used_bar_quantity,
                used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
            )
            trades.extend(close_result.trades)
            all_events.extend(close_result.events)
            cash += close_result.cash_delta
            if positions:
                raise ValueError(
                    "cannot force-close all positions at the backtest end under "
                    "available price/volume constraints: "
                    f"{sorted(positions)}"
                )
            # WHY: forced liquidation happens after the bar snapshot. Replace
            # that point so the curve, metrics, and final account cash reconcile
            # without creating a duplicate timestamp.
            if equity_curve:
                equity_curve[-1] = EquitySnapshot(ts=last_ts, equity=cash)
            terminal_events = [event for event in all_events if event.ts == last_ts]
            portfolio_snapshots[-1] = self._snapshot_portfolio(
                last_ts,
                {},
                last_prices,
                cash,
                (
                    sum(event.notional for event in terminal_events) / cash
                    if cash > EPSILON
                    else 0.0
                ),
                exposed=portfolio_snapshots[-1].exposed,
            )
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
                        {},
                        last_prices,
                        cash,
                        active_target_weights,
                    )
                )

        self._result = BacktestResult(
            trades=trades,
            order_events=all_events,
            position_snapshots=position_snapshots,
            allocation_snapshots=allocation_snapshots,
            funding_cash_flows=funding_cash_flows,
            account=AccountBacktestResult(
                account_id=self._account_id,
                currency=self._currency,
                equity_curve=tuple(equity_curve),
                portfolio_snapshots=tuple(portfolio_snapshots),
                initial_cash=self._initial_cash,
                final_equity=cash,
                exposed_periods=exposed_periods,
            ),
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

        When config is provided, perf_params come from config.
        annualize kwarg overrides config.reporting.annualize if explicitly passed.

        Raises RuntimeError if called before run().
        """
        from librae.backtest.schema import (
            AccountPerformance,
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
        timeframe = self._timeframe

        # Benchmark — computed here, not in run() (analysis config, not trade facts)
        benchmark_curve = self._compute_benchmark()

        # Resolve perf params from config or explicit args
        perf_kwargs: dict = {}
        if self._config is not None:
            perf_kwargs = self._config.perf_params.copy()
        if annualize is not None:
            perf_kwargs["annualize"] = annualize
        elif "annualize" not in perf_kwargs:
            perf_kwargs["annualize"] = False

        account = result.account
        trade_pnls = [
            TradePnL(
                gross_pnl=trade.gross_pnl,
                net_pnl=trade.net_pnl,
                commission=trade.commission,
                slippage=trade.slippage,
                tax=trade.tax,
                gross_return=trade.gross_return,
                net_return=trade.net_return,
                exit_commission=0.0,
                exit_slippage=0.0,
                exit_tax=trade.tax,
            )
            for trade in result.trades
        ]
        metrics = compute_all(
            equity_values=[snapshot.equity for snapshot in account.equity_curve],
            timestamps=[snapshot.ts for snapshot in account.equity_curve],
            trade_pnls=trade_pnls,
            total_periods=len(account.equity_curve),
            benchmark_values=benchmark_curve,
            exposed_periods=account.exposed_periods,
            trade_quantities=[trade.quantity for trade in result.trades],
            trade_notionals=[
                abs(
                    trade.entry_price
                    * trade.quantity
                    * self._get_cost_model(trade.symbol).multiplier
                )
                for trade in result.trades
            ],
            turnover_values=[snapshot.turnover for snapshot in account.portfolio_snapshots],
            gross_exposure_values=[
                snapshot.gross_exposure for snapshot in account.portfolio_snapshots
            ],
            net_exposure_values=[snapshot.net_exposure for snapshot in account.portfolio_snapshots],
            concentration_values=[
                snapshot.concentration for snapshot in account.portfolio_snapshots
            ],
            **perf_kwargs,
        )
        account_output = AccountPerformance(
            account_id=account.account_id,
            currency=account.currency,
            initial_cash=account.initial_cash,
            final_equity=account.final_equity,
            net_pnl=account.final_equity - account.initial_cash,
            equity_curve=tuple(
                self._enrich_equity_curve(
                    account.equity_curve,
                    account.portfolio_snapshots,
                    benchmark_curve,
                )
            ),
            metrics=metrics,
        )
        self._metrics = metrics

        run_metadata = RunMetadata(
            run_id=run_id,
            strategy=self._strategy_name,
            symbols=tuple(self._symbols),
            timeframe=timeframe,
            data_source=self._data_source,
            started_at=started_at,
            ended_at=ended_at,
            run_at=datetime.now(tz=UTC),
        )

        event_records = self._build_event_records(result, run_id)
        position_snapshot_points = self._build_position_snapshot_records(result)
        allocation_snapshot_points = self._build_allocation_snapshot_records(result)
        funding_cash_flow_records = self._build_funding_cash_flow_records(result)

        return BacktestOutput(
            run_metadata=run_metadata,
            account=account_output,
            order_events=tuple(event_records),
            position_snapshots=tuple(position_snapshot_points),
            allocation_snapshots=tuple(allocation_snapshot_points),
            funding_cash_flows=tuple(funding_cash_flow_records),
        )

    @property
    def metrics(self) -> StrategyMetrics:
        if self._metrics is None:
            raise RuntimeError("Call build_output() before accessing metrics")
        return self._metrics

    def _build_event_records(
        self,
        result: BacktestResult,
        run_id: str,
    ) -> list[OrderEventRecord]:
        """Map OrderEvent -> OrderEventRecord."""
        from librae.backtest.schema import OrderEventRecord

        return [
            OrderEventRecord(
                event_id=make_event_id(run_id, i),
                ts=e.ts,
                account_id=self._account_id,
                currency=self._currency,
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
                entry_commission=(
                    float(e.entry_commission) if e.entry_commission is not None else None
                ),
                entry_slippage=(float(e.entry_slippage) if e.entry_slippage is not None else None),
                entry_tax=float(e.entry_tax) if e.entry_tax is not None else None,
                pnl=float(e.pnl) if e.pnl is not None else None,
                net_return=float(e.net_return) if e.net_return is not None else None,
                entry_at=e.entry_at,
                periods_held=e.periods_held,
                reason=e.reason,
            )
            for i, e in enumerate(result.order_events)
        ]

    def _build_position_snapshot_records(
        self,
        result: BacktestResult,
    ) -> list[PositionSnapshotPoint]:
        """Map raw engine position snapshots to the canonical output schema."""
        from librae.backtest.schema import PositionSnapshotPoint

        return [
            PositionSnapshotPoint(
                ts=snapshot.ts,
                account_id=self._account_id,
                currency=self._currency,
                symbol=snapshot.symbol,
                side=snapshot.side,
                quantity=float(snapshot.quantity),
                price=float(snapshot.price),
                market_value=float(snapshot.market_value),
                realized_weight=float(snapshot.realized_weight),
            )
            for snapshot in result.position_snapshots
        ]

    def _build_allocation_snapshot_records(
        self,
        result: BacktestResult,
    ) -> list[AllocationSnapshotPoint]:
        """Map target-versus-achieved allocation facts to output schema."""
        from librae.backtest.schema import AllocationSnapshotPoint

        return [
            AllocationSnapshotPoint(
                ts=snapshot.ts,
                account_id=self._account_id,
                currency=self._currency,
                symbol=snapshot.symbol,
                target_weight=snapshot.target_weight,
                realized_weight=float(snapshot.realized_weight),
                weight_drift=snapshot.weight_drift,
            )
            for snapshot in result.allocation_snapshots
        ]

    def _build_funding_cash_flow_records(
        self,
        result: BacktestResult,
    ) -> list[FundingCashFlowRecord]:
        """Map funding cash flows to the canonical output schema."""
        from librae.backtest.schema import FundingCashFlowRecord

        return [
            FundingCashFlowRecord(
                ts=cash_flow.ts,
                account_id=self._account_id,
                currency=self._currency,
                symbol=cash_flow.symbol,
                side=cash_flow.side,
                quantity=cash_flow.quantity,
                mark_price=cash_flow.mark_price,
                multiplier=cash_flow.multiplier,
                rate=cash_flow.rate,
                cash_flow=cash_flow.cash_flow,
            )
            for cash_flow in result.funding_cash_flows
        ]

    @staticmethod
    def _enrich_equity_curve(
        equity_curve: Sequence[EquitySnapshot],
        portfolio_snapshots: Sequence[PortfolioSnapshot],
        benchmark_curve: list[float] | None,
    ) -> list[EquityCurvePoint]:
        """Build EquityCurvePoints with drawdown, period_return, and benchmark alignment."""
        from librae.backtest.schema import EquityCurvePoint

        has_bm = benchmark_curve is not None and len(benchmark_curve) > 0
        equity_points: list[EquityCurvePoint] = []
        peak = 0.0
        prev_eq = equity_curve[0].equity if equity_curve else 1.0
        prev_bm = float(benchmark_curve[0]) if has_bm else 1.0
        for i, snap in enumerate(equity_curve):
            portfolio = portfolio_snapshots[i]
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
                    exposed=portfolio.exposed,
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
        return (self._initial_cash * aligned / initial_price).tolist()

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

    def _precompute_lagged_adv(self) -> dict[pd.Timestamp, dict[str, float]]:
        """Precompute point-in-time ADV from completed trading sessions."""
        lookback = self._adv_lookback_sessions
        if lookback is None:
            return {}

        result: dict[pd.Timestamp, dict[str, float]] = {}
        for symbol, symbol_data in self._data.groupby(level="symbol", sort=False):
            volume = symbol_data["volume"].droplevel("symbol")
            labels = None
            if self._timeframe != "D1":
                calendar_id = self._calendar_ids[symbol]
                if calendar_id is None:  # guarded at run() boundary
                    raise RuntimeError(f"missing calendar_id for {symbol}")
                labels = session_labels(pd.DatetimeIndex(volume.index), calendar_id)
            lagged = calculate_lagged_adv(
                volume,
                lookback,
                session_labels=labels,
            )
            for ts, value in lagged.dropna().items():
                result.setdefault(ts, {})[symbol] = float(value)
        return result

    def _precompute_session_labels(self) -> dict[pd.Timestamp, dict[str, object]]:
        """Map every bar to its instrument session for cumulative ADV usage."""
        if self._adv_lookback_sessions is None:
            return {}

        result: dict[pd.Timestamp, dict[str, object]] = {}
        for symbol, symbol_data in self._data.groupby(level="symbol", sort=False):
            timestamps = pd.DatetimeIndex(symbol_data.index.get_level_values("datetime"))
            if self._timeframe == "D1":
                labels: Iterable[object] = timestamps
            else:
                calendar_id = self._calendar_ids[symbol]
                if calendar_id is None:  # guarded at run() boundary
                    raise RuntimeError(f"missing calendar_id for {symbol}")
                labels = session_labels(timestamps, calendar_id)
            for timestamp, label in zip(timestamps, labels, strict=True):
                result.setdefault(timestamp, {})[symbol] = label
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

    def _calc_equity_snapshot(
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

        return calc_equity(
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
        signed_notionals = calculate_signed_position_notionals(
            positions,
            prices=last_prices,
            get_cost_model=self._get_cost_model,
        )
        realized_weights = calculate_position_weights(
            positions,
            equity,
            prices=last_prices,
            get_cost_model=self._get_cost_model,
        )
        for symbol in sorted(positions):
            position = positions[symbol]
            price = last_prices[symbol]
            snapshots.append(
                PositionSnapshot(
                    ts=ts,
                    symbol=symbol,
                    side=position.side,
                    quantity=position.quantity,
                    price=price,
                    market_value=signed_notionals[symbol],
                    realized_weight=realized_weights[symbol],
                )
            )
        return snapshots

    def _realized_weights(
        self,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
    ) -> dict[str, float]:
        return calculate_position_weights(
            positions,
            equity,
            prices=last_prices,
            get_cost_model=self._get_cost_model,
        )

    def _snapshot_allocations(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
        target_weights: dict[str, float] | None,
    ) -> list[AllocationSnapshot]:
        """Record every configured symbol, including unfilled targets."""
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
        *,
        exposed: bool | None = None,
    ) -> PortfolioSnapshot:
        """Compute end-of-event exposure ratios from signed market values."""
        realized_weights = self._realized_weights(positions, last_prices, equity)
        return PortfolioSnapshot(
            ts=ts,
            gross_exposure=sum(abs(weight) for weight in realized_weights.values()),
            net_exposure=sum(realized_weights.values()),
            concentration=max((abs(weight) for weight in realized_weights.values()), default=0.0),
            turnover=turnover,
            exposed=bool(positions) if exposed is None else exposed,
        )

    @staticmethod
    def _filled_quantities(events: Iterable[OrderEvent]) -> dict[str, float]:
        """Aggregate quantity already matched per symbol in one data event."""
        quantities: dict[str, float] = {}
        for event in events:
            quantities[event.symbol] = quantities.get(event.symbol, 0.0) + event.fill_quantity
        return quantities
