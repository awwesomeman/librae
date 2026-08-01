#!/usr/bin/env python3
"""Kill-switch rehearsal — issue #86 DoD: halt()/reset_halt() rehearsed
against a running paper session.

Runs a real LiveTrader against Binance sandbox (Demo Trading, mode=live),
places one small resting order, then calls the operator controls from
docs/guides/optional-infrastructure.md: trader.halt(reason), then
trader.reset_halt() after manual reconciliation.

Requires BINANCE_API_KEY/BINANCE_API_SECRET and BINANCE_SANDBOX=true
(e.g. in .credentials/<account>.env) — refuses to run otherwise.

Usage:
    uv run python scripts/rehearse_kill_switch.py
    uv run python scripts/rehearse_kill_switch.py --quantity 0.001 --run-seconds 75

If the sandbox account isn't flat (common — a dust balance below the
symbol's lot-size step can never clear via a market order), a first live
run fails closed with "Non-flat First-run Broker State". Pass
--seed-reviewed-state to seed a checkpoint matching the account's actual
broker state instead — strategy-readiness.md's "reviewed restored state"
alternative to a flat account.

Record the outcome in docs/guides/operational-runbook.md's rehearsal log.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime

from librae.core.run_config import AccountConfig, ExecutionPolicy, RunConfig, RuntimePolicy
from librae.core.strategy import Context, OrderIntent, PositionState, Strategy
from librae.live.state import LiveRuntimeState, MemoryLiveStateStore
from librae.orchestration.live import build_live_trader

SYMBOL = "ETHUSDT"
TIMEFRAME = "M1"
RUNTIME_REVISION = "ops-rehearsal-kill-switch"


class _RestingLimitOnceStrategy(Strategy):
    """Places one deep-limit buy order on the first bar, then holds.

    A market order would likely already be filled by the time halt() runs
    (nothing left to cancel), so this rests on the book instead. Tracks
    "already ordered" itself rather than via ctx.positions, since
    --seed-reviewed-state can restore a pre-existing dust position.
    """

    def __init__(self, quantity: float) -> None:
        self._quantity = quantity
        self._ordered = False

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if self._ordered:
            return []
        self._ordered = True
        # 0.6x clears Binance's PERCENT_PRICE_BY_SIDE floor (0.5x) with
        # margin, while staying far enough from market to never fill here.
        limit_price = ctx.bar["close"] * 0.6
        return [
            OrderIntent(
                action="long", symbol=ctx.symbol, quantity=self._quantity, limit_price=limit_price
            )
        ]


def _feature_fn(h1_base):
    df = h1_base.copy()
    df["entry_signal"] = False
    df["exit_signal"] = False
    return df


def _build_config() -> RunConfig:
    return RunConfig(
        strategy_name="ops_rehearsal_kill_switch",
        symbols=[SYMBOL],
        timeframe=TIMEFRAME,
        market="crypto",
        data_source="binance_spot",
        account=AccountConfig(currency="USDT", initial_cash=100_000.0),
        mode="live",
        broker="binance",
        # SYMBOL isn't in librae's built-in registry (only BTCUSDT is), so
        # its instrument metadata and multiplier need to be supplied here.
        instrument_overrides={SYMBOL: {"instrument_type": "spot", "currency": "USDT"}},
        cost_overrides={"multiplier": 1.0},
        # warmup_periods must be >= 2 for live fetching: limit=1 with
        # drop_incomplete=True can return the still-forming bar as the sole
        # row, which then gets dropped, leaving an empty frame every cycle.
        execution=ExecutionPolicy(max_bar_volume_participation_rate=None, warmup_periods=2),
        runtime=RuntimePolicy(poll_seconds=5),
    )


def _seed_reviewed_state(config: RunConfig, store: MemoryLiveStateStore) -> None:
    """Write a checkpoint matching the account's actual broker state.

    Mirrors what LiveTrader._read_broker_positions() reads, so the restore
    reconciliation branch sees a matching book instead of an unexplained
    drift.
    """
    from librae.brokers.crypto_adapter import CryptoAdapter, CryptoCredentials
    from librae.config.symbols import resolve_symbol
    from librae.core.cost_model import CostModel
    from librae.live.executor import PositionRequest

    adapter = CryptoAdapter(
        credentials=CryptoCredentials.from_env("BINANCE", exchange_id="binance")
    )
    instrument = resolve_symbol(
        config, SYMBOL, multiplier=CostModel.from_config(config, symbol=SYMBOL).multiplier
    )
    request = PositionRequest(
        symbol=SYMBOL,
        venue_symbol=instrument.venue_symbol,
        currency=instrument.currency,
        multiplier=instrument.multiplier,
        security_type=instrument.security_type,
        exchange=instrument.exchange,
        continuous_alias=instrument.continuous_alias,
        contract_month=instrument.contract_month,
    )
    raw_position = adapter.get_position(request)
    size = float(raw_position.get("size") or 0)
    cash = float(adapter._exchange.fetch_balance()["total"].get("USDT", 0.0))

    positions: dict[str, PositionState] = {}
    last_prices: dict[str, float] = {}
    position_value = 0.0
    if size:
        price = float(adapter._exchange.fetch_ticker(instrument.venue_symbol)["last"])
        position_value = price * abs(size)
        positions[SYMBOL] = PositionState(
            symbol=SYMBOL,
            side="long" if size > 0 else "short",
            entry_price=price,
            quantity=abs(size),
            entry_at=datetime.now(UTC),
            periods_held=0,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=position_value,
        )
        # reset_halt()'s equity calc needs a cached price for every open
        # position with no fallback — omitting this raises ValueError.
        last_prices[SYMBOL] = price
    equity = cash + position_value
    print(f"[kill-switch] seeding reviewed state: {SYMBOL} size={size}, USDT cash={cash}")

    state = LiveRuntimeState(
        state_key=f"{config.mode}:{config.config_hash}",
        run_id="ops_rehearsal_kill_switch_seeded",
        config_hash=config.config_hash,
        mode=config.mode,
        account_id=config.account.account_id,
        cash=cash,
        runtime_revision=RUNTIME_REVISION,
        positions=positions,
        last_prices=last_prices,
        equity_peak=equity,
        prev_equity=equity,
    )
    store.save(state)


def _require_sandbox() -> None:
    if os.environ.get("BINANCE_SANDBOX", "").lower() != "true":
        sys.exit(
            "BINANCE_SANDBOX must be 'true' to run this rehearsal — refusing to risk "
            "a mainnet account. Set it in your Binance credentials file (Demo Trading)."
        )
    if not os.environ.get("BINANCE_API_KEY") or not os.environ.get("BINANCE_API_SECRET"):
        sys.exit("BINANCE_API_KEY/BINANCE_API_SECRET are required (see .env.secrets.example).")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quantity", type=float, default=0.01, help=f"{SYMBOL} quantity to buy")
    parser.add_argument(
        "--run-seconds",
        type=int,
        default=75,
        help="seconds to run before halting (>60 recommended: on timeframe=M1, "
        "a new completed bar can be up to a minute away)",
    )
    parser.add_argument(
        "--seed-reviewed-state",
        action="store_true",
        help="seed a checkpoint matching the account's current broker state before starting "
        "(use when the sandbox account is not flat and can't be made flat — see module docstring)",
    )
    args = parser.parse_args()

    _require_sandbox()

    config = _build_config()
    state_store = MemoryLiveStateStore()
    if args.seed_reviewed_state:
        _seed_reviewed_state(config, state_store)

    trader = build_live_trader(
        _RestingLimitOnceStrategy(args.quantity),
        _feature_fn,
        config=config,
        database_enabled=False,
        telegram_config={"enabled": True},
        state_store=state_store,
        runtime_revision=RUNTIME_REVISION,
    )

    def _operator_actions() -> None:
        print(f"[kill-switch] running for {args.run_seconds}s to let an order open...")
        time.sleep(args.run_seconds)

        print("[kill-switch] calling trader.halt('kill-switch rehearsal')...")
        trader.halt("kill-switch rehearsal")
        active = len(trader._active_orders)
        print(f"[kill-switch] halted. active tracked orders after cancel attempt: {active}")

        print(
            "[kill-switch] confirm the halt in the Binance UI, then reconcile manually if needed."
        )
        input("[kill-switch] press Enter once reconciled, to call reset_halt()... ")

        try:
            trader.reset_halt()
            print("[kill-switch] reset_halt() succeeded — new risk epoch started.")
        except RuntimeError as exc:
            print(f"[kill-switch] reset_halt() refused (expected if orders are unresolved): {exc}")

        trader.stop()

    # trader.run() registers SIGTERM/SIGINT handlers, which only Python's main
    # thread may do — it must be the one blocking in run(), not the operator
    # actions above, which is why those are threaded off instead.
    operator_thread = threading.Thread(target=_operator_actions, daemon=True)
    operator_thread.start()
    trader.run()
    operator_thread.join(timeout=5)
    print("[kill-switch] done — record the outcome in docs/guides/operational-runbook.md.")


if __name__ == "__main__":
    main()
