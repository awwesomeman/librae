"""Deployment wiring for Librae's injected live engine."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pandas as pd

from librae.config.symbols import resolve_symbol
from librae.core.cost_model import CostModel
from librae.core.utils import make_event_id
from librae.integrations import AdapterFactory
from librae.live.engine import LiveTrader
from librae.live.interfaces import Notifier
from librae.live.state import normalize_runtime_revision

if TYPE_CHECKING:
    from librae.config.symbols import SymbolInfo
    from librae.core.executor import PositionEvent, RuntimeEvent
    from librae.core.financing import FinancingCashFlow
    from librae.core.run_config import RunConfig
    from librae.core.strategy import Strategy
    from librae.live.state import LiveStateStore

logger = logging.getLogger(__name__)

_DATA_ADAPTER_BY_BROKER = {
    "binance": "crypto",
    "ibkr": "ibkr",
    "shioaji": "shioaji",
}
_DB_FAILURE_ALERT_THRESHOLD = 3
_READY_FILE_ENV = "LIBRAE_READY_FILE"
_READY_TOKEN_ENV = "LIBRAE_READY_TOKEN"


def _ready_callback_from_env() -> Callable[[str], None] | None:
    ready_file = os.environ.get(_READY_FILE_ENV)
    if not ready_file:
        return None
    path = Path(ready_file)
    ready_token = os.environ.get(_READY_TOKEN_ENV)

    def mark_ready(run_id: str) -> None:
        marker_parts = [run_id, uuid4().hex]
        if ready_token:
            marker_parts.insert(0, ready_token)
        path.write_text(":".join(marker_parts) + "\n", encoding="utf-8")

    return mark_ready


def _combine_ready_callbacks(
    callback: Callable[[str], None] | None,
) -> Callable[[str], None] | None:
    deployment_callback = _ready_callback_from_env()
    if callback is None:
        return deployment_callback
    if deployment_callback is None:
        return callback

    def notify_ready(run_id: str) -> None:
        callback(run_id)
        deployment_callback(run_id)

    return notify_ready


def _build_adapter(
    name: str,
    *,
    trading: bool,
    factories: Mapping[str, AdapterFactory] | None = None,
    instrument_type: str | None = None,
) -> object:
    """Construct one explicitly configured repository adapter.

    ``instrument_type`` picks the CCXT venue for crypto: Binance splits spot
    and USDS-margined contracts (spot/perpetual/dated futures) into separate
    exchange ids, so a contract symbol built against the default spot venue
    can't resolve its own market (e.g. ``BTC/USDT:USDT`` doesn't exist on
    ``ccxt.binance``).
    """
    factory = (factories or {}).get(name)
    if factory is not None:
        return factory(trading=trading)
    if name == "shioaji":
        from librae.brokers.shioaji_adapter import ShioajiAdapter

        return ShioajiAdapter()
    if name == "ibkr":
        from librae.brokers.ibkr_adapter import IBKRAdapter

        return IBKRAdapter(trading_enabled=trading)
    if name in ("crypto", "binance"):
        from librae.brokers.crypto_adapter import CryptoAdapter, CryptoCredentials

        exchange_id = "binance" if instrument_type in (None, "spot") else "binanceusdm"
        credentials = (
            CryptoCredentials.from_env("BINANCE", exchange_id=exchange_id) if trading else None
        )
        return CryptoAdapter(exchange_id=exchange_id, credentials=credentials)
    raise ValueError(f"unsupported adapter: {name!r}")


def _build_notifier(config: Mapping[str, object] | None) -> Notifier | None:
    if not config or not config.get("enabled", False):
        return None

    from librae.notifications.config import TelegramConfig
    from librae.notifications.telegram import TelegramAdapter, TelegramCredentials

    return TelegramAdapter(
        config=TelegramConfig.from_dict(dict(config or {})),
        credentials=TelegramCredentials.from_env("TELEGRAM"),
    )


def _status_interval_periods(config: Mapping[str, object] | None) -> int | None:
    if not config or not config.get("enabled", False):
        return None

    from librae.notifications.config import TelegramConfig

    status = TelegramConfig.from_dict(dict(config)).notifications.status
    return status.interval_periods if status.enabled else None


def _build_state_store() -> object:
    try:
        from librae.db.timescale_state import TimescaleLiveStateStore
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "TimescaleDB persistence is enabled but its optional dependencies "
            "are unavailable. Install Librae's 'db' extra or disable repository "
            "database wiring."
        ) from exc
    return TimescaleLiveStateStore()


class _TimescaleCallbacks:
    """Best-effort analytics sink; runtime checkpoints remain fail-fast."""

    def __init__(
        self,
        config: RunConfig,
        instruments: dict[str, SymbolInfo],
        notifier: Notifier | None,
    ) -> None:
        self._config = config
        self._instruments = instruments
        self._notifier = notifier
        self._run_id = ""
        self._failures: dict[str, int] = {}

    def _write(
        self,
        callback: Callable[..., object],
        *args: object,
        critical: bool = False,
        **kwargs: object,
    ) -> None:
        """Run one best-effort DB write.

        ``critical=True`` alerts on the first failure instead of after
        ``_DB_FAILURE_ALERT_THRESHOLD`` consecutive ones — for writes that
        are one-shot (register_run fires once per process, so it would
        never reach a consecutive-failure threshold at all) or per-event
        and irreplaceable (a fill, a funding payment), unlike a recoverable
        next-bar snapshot such as equity_curve.
        """
        name = callback.__name__
        try:
            callback(*args, **kwargs)
            self._failures[name] = 0
        except Exception as exc:
            failures = self._failures.get(name, 0) + 1
            self._failures[name] = failures
            logger.warning(
                "DB %s failed (%d consecutive): %s",
                name,
                failures,
                exc,
            )
            threshold = 1 if critical else _DB_FAILURE_ALERT_THRESHOLD
            if failures == threshold:
                self._alert(
                    title=f"[{self._config.strategy_name}] DB Write Failing",
                    message=(f"{failures} consecutive {name} failures; trading continues."),
                )

    def _alert(self, *, title: str, message: str) -> None:
        notifier = self._notifier
        if notifier is None or not bool(getattr(notifier, "enabled", False)):
            return
        try:
            notifier.send_alert(title=title, message=message)
        except Exception:
            logger.exception("DB failure notification failed")

    def register_run(self, run_id: str) -> None:
        from librae.backtest.schema import StrategyMetrics
        from librae.db.timescale_writer import write_run_metadata, write_strategy_performance

        self._run_id = run_id
        self._write(
            write_run_metadata,
            critical=True,
            run_id=run_id,
            strategy_name=self._config.strategy_name,
            symbols=self._config.symbols,
            timeframe=self._config.timeframe,
            mode=self._config.mode,
            started_at=datetime.now(tz=UTC),
            data_source=self._config.data_source,
            poll_seconds=self._config.runtime.poll_seconds,
            params=self._config.params,
            execution_policy=asdict(self._config.execution),
            risk_policy=asdict(self._config.risk),
        )
        # Seed a $0/0.0% baseline row up front — otherwise strategy_performance
        # has no row at all until the first close/reduce (on_performance is
        # dirty-flag gated, see live/engine.py), so every KPI panel sourced
        # from it (Total Return, Max Drawdown, Sharpe, ...) shows "No data"
        # next to an already-live Unrealized P&L, which reads as broken.
        self._write(
            write_strategy_performance,
            critical=True,
            run_id=run_id,
            account_id=self._config.account_id,
            currency=self._config.account.currency,
            initial_cash=self._config.account.initial_cash,
            final_equity=self._config.account.initial_cash,
            net_pnl=0.0,
            metrics=StrategyMetrics(total_return=0.0, max_drawdown=0.0, trades=0),
        )

    def on_bar(
        self,
        run_id: str,
        ts: datetime,
        account_id: str,
        currency: str,
        equity: float,
        drawdown: float,
        period_return: float,
        gross_exposure: float,
        net_exposure: float,
        concentration: float,
        turnover: float,
    ) -> None:
        from librae.db.timescale_writer import write_equity_curve_point

        self._write(
            write_equity_curve_point,
            ts=ts,
            run_id=run_id,
            account_id=account_id,
            currency=currency,
            equity=equity,
            drawdown=drawdown,
            period_return=period_return,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            concentration=concentration,
            turnover=turnover,
            exposed=gross_exposure > 0,
            strategy_name=self._config.strategy_name,
        )

    def on_position_event(self, event: PositionEvent, sequence: int) -> None:
        from librae.db.timescale_writer import write_trade_event

        fields = asdict(event)
        fields.update(
            event_id=make_event_id(self._run_id, sequence),
            run_id=self._run_id,
            strategy_name=self._config.strategy_name,
            mode=self._config.mode,
            timeframe=self._config.timeframe,
            account_id=self._config.account_id,
            currency=self._config.account.currency,
        )
        self._write(write_trade_event, critical=True, **fields)

    def on_financing_cash_flow(self, cash_flow: FinancingCashFlow) -> None:
        from librae.db.timescale_writer import write_financing_cash_flow

        self._write(
            write_financing_cash_flow,
            critical=True,
            run_id=self._run_id,
            account_id=self._config.account_id,
            currency=self._config.account.currency,
            ts=cash_flow.ts,
            symbol=cash_flow.symbol,
            kind=cash_flow.kind,
            side=cash_flow.side,
            quantity=cash_flow.quantity,
            mark_price=cash_flow.mark_price,
            multiplier=cash_flow.multiplier,
            rate=cash_flow.rate,
            cash_flow=cash_flow.cash_flow,
            group_id=cash_flow.group_id,
            entry_at=cash_flow.entry_at,
        )

    def on_runtime_event(self, event: RuntimeEvent) -> None:
        from librae.db.timescale_writer import write_runtime_event

        self._write(
            write_runtime_event,
            run_id=self._run_id,
            ts=event.ts,
            event_type=event.event_type,
            symbol=event.symbol,
            detail=event.detail,
        )

    def on_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        bar: dict[str, float],
        ts: datetime,
    ) -> None:
        from librae.db.timescale_writer import write_ohlcv

        frame = pd.DataFrame(
            [
                {
                    "ts": ts,
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                }
            ]
        ).set_index("ts")
        instrument = self._instruments[symbol]
        self._write(
            write_ohlcv,
            frame,
            symbol,
            timeframe,
            data_source=instrument.data_source,
            instrument_type=instrument.instrument_type,
        )

    def on_heartbeat(self, run_id: str) -> None:
        from librae.db.timescale_writer import update_heartbeat

        self._write(update_heartbeat, run_id)

    def on_signal_outcome(
        self,
        symbol: str,
        ts: datetime,
        signal_value: float,
        price: float,
        signal_type: str = "entry",
    ) -> None:
        from librae.db.timescale_writer import write_signal_event

        self._write(
            write_signal_event,
            ts=ts,
            run_id=self._run_id,
            strategy_name=self._config.strategy_name,
            symbol=symbol,
            mode=self._config.mode,
            timeframe=self._config.timeframe,
            signal_value=signal_value,
            price=price,
            signal_type=signal_type,
        )

    def on_performance(self, run_id: str, account_id: str) -> None:
        from librae.db.timescale_writer import refresh_performance

        self._write(refresh_performance, run_id, account_id, config=self._config)


def build_live_trader(
    strategy_name: Strategy,
    feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    config: RunConfig,
    database_enabled: bool = True,
    telegram_config: Mapping[str, object] | None = None,
    adapter_factories: Mapping[str, AdapterFactory] | None = None,
    data_adapter_overrides: Mapping[str, object] | None = None,
    notifier: Notifier | None = None,
    status_interval_periods: int | None = None,
    state_store: LiveStateStore | None = None,
    runtime_revision: str | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> LiveTrader:
    """Build a sim/live deployment from built-in or caller-registered factories.

    ``data_adapter_overrides`` injects a concrete market-data adapter
    instance for specific symbols directly, bypassing ``adapter_factories``
    (which resolves by adapter *type*, not per symbol) — for a symbol that
    needs a bespoke data source ``instrument_overrides``' ``data_adapter``
    type can't express.
    """
    resolved_runtime_revision = normalize_runtime_revision(
        runtime_revision,
        required=config.mode == "live",
    )
    factories = dict(adapter_factories or {})
    if notifier is not None and telegram_config:
        raise ValueError("inject notifier or configure Telegram, not both")
    resolved_notifier = notifier or _build_notifier(telegram_config)
    resolved_status_interval = (
        status_interval_periods
        if status_interval_periods is not None
        else _status_interval_periods(telegram_config)
    )
    cost_models = {
        symbol: CostModel.from_config(config, symbol=symbol) for symbol in config.symbols
    }
    instruments = {
        symbol: resolve_symbol(
            config,
            symbol,
            multiplier=cost_models[symbol].multiplier,
        )
        for symbol in config.symbols
    }

    overrides = dict(data_adapter_overrides or {})
    adapter_instances: dict[tuple[str, str, str], object] = {}
    data_adapters: dict[str, object] = {}
    for symbol, instrument in instruments.items():
        if symbol in overrides:
            data_adapters[symbol] = overrides[symbol]
            continue
        if instrument.data_adapter == "crypto" and instrument.continuous_alias:
            raise ValueError(
                f"{symbol!r} is a continuous crypto alias and is not directly orderable; "
                "configure a concrete venue_symbol or inject a custom adapter into LiveTrader"
            )
        route = (config.instrument_overrides or {}).get(symbol, {})
        broker = route.get("broker") or config.broker
        trading = (
            config.mode == "live"
            and broker is not None
            and _DATA_ADAPTER_BY_BROKER.get(broker, broker) == instrument.data_adapter
        )
        key = (instrument.data_adapter, instrument.data_source, instrument.instrument_type)
        instance = adapter_instances.get(key)
        if instance is None:
            instance = _build_adapter(
                instrument.data_adapter,
                trading=trading,
                factories=factories,
                instrument_type=instrument.instrument_type,
            )
            adapter_instances[key] = instance
        data_adapters[symbol] = instance

    unknown_overrides = set(overrides) - set(instruments)
    if unknown_overrides:
        raise ValueError(f"data_adapter_overrides has unknown symbols: {sorted(unknown_overrides)}")

    order_adapters: dict[str, object] | None = None
    if config.mode == "live":
        broker_instances: dict[str, object] = {}
        order_adapters = {}
        for symbol, instrument in instruments.items():
            route = (config.instrument_overrides or {}).get(symbol, {})
            broker = route.get("broker") or config.broker
            if not broker:
                raise ValueError(
                    f"live execution broker is not configured for {symbol!r}; "
                    "set strategy.broker or instrument_overrides"
                )
            adapter_name = _DATA_ADAPTER_BY_BROKER.get(broker, broker)
            if adapter_name not in _DATA_ADAPTER_BY_BROKER.values() and broker not in factories:
                raise ValueError(f"unsupported execution broker: {broker!r}")
            instance = broker_instances.get(broker)
            if instance is None:
                data_instance = data_adapters[symbol]
                instance = (
                    data_instance
                    if adapter_name == instrument.data_adapter
                    else _build_adapter(
                        broker,
                        trading=True,
                        factories=factories,
                    )
                )
                broker_instances[broker] = instance
            order_adapters[symbol] = instance

    resolved_state_store = state_store
    if resolved_state_store is None and database_enabled:
        resolved_state_store = _build_state_store()
    callbacks = (
        _TimescaleCallbacks(config, instruments, resolved_notifier) if database_enabled else None
    )
    trader = LiveTrader(
        strategy_name,
        feature_fn,
        config=config,
        adapter=data_adapters,
        order_adapter=order_adapters,
        cost_model=cost_models,
        notifier=resolved_notifier,
        status_interval_periods=resolved_status_interval,
        state_store=resolved_state_store,
        runtime_revision=resolved_runtime_revision,
        on_bar=callbacks.on_bar if callbacks else None,
        on_position_event=callbacks.on_position_event if callbacks else None,
        on_ohlcv=callbacks.on_ohlcv if callbacks else None,
        on_heartbeat=callbacks.on_heartbeat if callbacks else None,
        on_signal_outcome=callbacks.on_signal_outcome if callbacks else None,
        on_financing_cash_flow=callbacks.on_financing_cash_flow if callbacks else None,
        on_runtime_event=callbacks.on_runtime_event if callbacks else None,
        on_performance=callbacks.on_performance if callbacks else None,
        on_ready=_combine_ready_callbacks(on_ready),
        # Must run before LiveTrader's own first checkpoint write, which a
        # durable state_store may reject until the run is registered (e.g. a
        # foreign key to a run-metadata table) — calling this only after
        # construction returns is too late, since __init__ already persisted.
        on_run_registered=callbacks.register_run if callbacks else None,
    )
    return trader
