"""Trade a mean-reverting spread with an explicitly sized order group."""

from __future__ import annotations

from math import isfinite

import pandas as pd
from librae import Context, MultiLegOrder, OrderIntent, Strategy, StrategyDecision


def prepare_signals(
    df: pd.DataFrame,
    near_symbol: str,
    far_symbol: str,
    *,
    lookback: int = 20,
    hedge_ratio: float = 1.0,
) -> pd.DataFrame:
    """Add one point-in-time spread z-score to both leg rows."""
    if lookback < 2:
        raise ValueError("lookback must be at least 2")
    if not isfinite(hedge_ratio) or hedge_ratio <= 0:
        raise ValueError("hedge_ratio must be finite and positive")

    out = df.copy()
    closes = out["close"].unstack("symbol")
    missing = {near_symbol, far_symbol} - set(closes)
    if missing:
        raise ValueError(f"spread data is missing symbols: {sorted(missing)}")
    spread = closes[near_symbol] - hedge_ratio * closes[far_symbol]
    rolling = spread.rolling(lookback, min_periods=lookback)
    spread_std = rolling.std(ddof=1)
    zscore = (spread - rolling.mean()) / spread_std.where(spread_std > 0)
    timestamps = out.index.get_level_values("datetime")
    out["spread_zscore"] = timestamps.map(zscore)
    return out


class MultiLegSpreadStrategy(Strategy):
    """Open and close a two-leg spread as one best-effort decision group."""

    def __init__(
        self,
        near_symbol: str,
        far_symbol: str,
        *,
        quantity: float = 10.0,
        hedge_ratio: float = 1.0,
        entry_zscore: float = 1.5,
        exit_zscore: float = 0.25,
    ) -> None:
        if near_symbol == far_symbol:
            raise ValueError("spread symbols must differ")
        if not isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be finite and positive")
        if not isfinite(hedge_ratio) or hedge_ratio <= 0:
            raise ValueError("hedge_ratio must be finite and positive")
        far_quantity = quantity * hedge_ratio
        if not isfinite(far_quantity):
            raise ValueError("quantity times hedge_ratio must be finite")
        if not isfinite(entry_zscore) or entry_zscore <= 0:
            raise ValueError("entry_zscore must be finite and positive")
        if not isfinite(exit_zscore) or not 0 <= exit_zscore < entry_zscore:
            raise ValueError("exit_zscore must be in [0, entry_zscore)")
        self._near_symbol = near_symbol
        self._far_symbol = far_symbol
        self._quantity = quantity
        self._far_quantity = far_quantity
        self._entry_zscore = entry_zscore
        self._exit_zscore = exit_zscore

    def on_bar(self, ctx: Context) -> StrategyDecision:
        required = {self._near_symbol, self._far_symbol}
        if not required.issubset(ctx.available_symbols):
            return []
        raw_zscore = ctx.bars[self._near_symbol].get("spread_zscore")
        if raw_zscore is None or not isfinite(raw_zscore):
            return []
        zscore = float(raw_zscore)

        near_position = ctx.positions.get(self._near_symbol)
        far_position = ctx.positions.get(self._far_symbol)
        if near_position is None and far_position is None and abs(zscore) >= self._entry_zscore:
            near_action = "short" if zscore > 0 else "long"
            far_action = "long" if zscore > 0 else "short"
            return MultiLegOrder(
                legs=(
                    OrderIntent(
                        action=near_action,
                        symbol=self._near_symbol,
                        quantity=self._quantity,
                    ),
                    OrderIntent(
                        action=far_action,
                        symbol=self._far_symbol,
                        quantity=self._far_quantity,
                    ),
                ),
                reason="spread_entry",
            )

        if (
            near_position is not None
            and far_position is not None
            and abs(zscore) <= self._exit_zscore
        ):
            return MultiLegOrder(
                legs=(
                    OrderIntent(
                        action="close",
                        symbol=self._near_symbol,
                        quantity=near_position.quantity,
                    ),
                    OrderIntent(
                        action="close",
                        symbol=self._far_symbol,
                        quantity=far_position.quantity,
                    ),
                ),
                reason="spread_exit",
            )
        return []
