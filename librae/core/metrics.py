"""Performance metrics module — QuantStats adapter + custom metrics.

Standard metrics (Sharpe, Sortino, MDD, Calmar, etc.) delegated to QuantStats,
which always uses ddof=1 internally (not configurable — verified against
quantstats.stats.sharpe source, so this project doesn't expose a fake ddof knob).
Custom metrics not in QuantStats (exposure_ratio, avg_hold_periods) computed here.
compute_all() is the single entry point, accepts primitive sequences.

The annualization factor fed to QuantStats is always inferred from actual bar
density (see _infer_annual_periods) so intraday timeframes annualize correctly.
annual_periods (trading days/year, e.g. 365 for crypto, 252 for TW) is only a
fallback for when density can't be inferred (<2 bars).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from matplotlib.axes import Axes

    from librae.backtest.schema import BacktestOutput, OrderEventRecord, StrategyMetrics
    from librae.core.executor import TradePnL

from librae.core import EPSILON

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 365.25 * 86400


def _infer_annual_periods(index: pd.DatetimeIndex, fallback: int) -> int:
    """Infer how many bars fit in one year from actual data density.

    Uses ``n_bars / span_years`` — the true observed bar rate — so it
    correctly handles markets with limited trading hours (e.g. 5h/day
    futures) and any bar frequency (H1, D1, W1, etc.). Falls back to
    ``fallback`` (the configured trading-days/year) when there isn't
    enough data to infer a rate from.
    """
    if len(index) < 2:
        return fallback
    span_seconds = (index[-1] - index[0]).total_seconds()
    if span_seconds <= 0:
        return fallback
    span_years = span_seconds / SECONDS_PER_YEAR
    return max(1, round(len(index) / span_years))


def compute_all(
    equity_values: Sequence[float],
    timestamps: Sequence[datetime],
    trade_pnls: Sequence[TradePnL],
    total_periods: int,
    annualize: bool = False,
    benchmark_values: Sequence[float] | None = None,
    exposed_periods: int | None = None,
    trade_quantities: Sequence[float] | None = None,
    risk_free_rate: float = 0.0,
    annual_periods: int = 365,
) -> StrategyMetrics:
    """Compute all metrics from equity curve + trades.

    Args:
        equity_values: Raw equity values per bar.
        timestamps: Corresponding timestamps (used for annualization).
        trade_pnls: TradePnL from core.executor.calc_trade_pnl().
        total_periods: Total bar count (for exposure_ratio).
        annualize: If True, compute annualized metrics.
        benchmark_values: Buy-and-hold equity values for benchmark comparison.
        exposed_periods: Number of bars with at least one open position.
        trade_quantities: Per-trade closed quantity (for quantity-weighted avg return).
        risk_free_rate: Annual risk-free rate (crypto=0, TW=0.015).
        annual_periods: Trading days per year (crypto=365, TW=252). Used only
            as a fallback when bar density can't be inferred from timestamps
            (<2 bars) — otherwise the real annualization factor is inferred
            from actual bar density, see _infer_annual_periods.

    Called once by backtest (at build_output time).
    Called periodically by live (based on monitoring frequency).
    """
    # WHY: lazy imports — quantstats pulls in matplotlib/scipy (~1-3s),
    # deferred so `import librae` stays fast.
    import quantstats as qs

    from librae.backtest.schema import StrategyMetrics

    if not equity_values:
        return StrategyMetrics(total_return=0.0, trades=0)

    eq_arr = np.array(equity_values, dtype=np.float64)
    # WHY: QuantStats max_drawdown/calmar require DatetimeIndex, not RangeIndex
    ts_index = pd.DatetimeIndex(timestamps[1:]) if len(timestamps) > 1 else pd.DatetimeIndex([])
    returns = pd.Series(
        np.diff(eq_arr) / (eq_arr[:-1] + EPSILON),
        index=ts_index,
        dtype=np.float64,
    )

    n_trades = len(trade_pnls)
    if n_trades == 0 or len(returns) < 2:
        _comp = _safe_qs(qs.stats.comp, returns) if len(returns) > 0 else None
        total_ret = _comp if _comp is not None else 0.0
        return StrategyMetrics(total_return=total_ret, trades=n_trades)

    _dd = _safe_qs(qs.stats.max_drawdown, returns)
    max_dd = _dd if _dd is not None else 0.0

    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    ann_return: float | None = None
    if annualize:
        # WHY: QuantStats expects bars-per-year, not trading-days-per-year,
        # so annualization always uses actual bar density (correct for any
        # timeframe — H1, D1, ...); annual_periods is only the fallback for
        # when density can't be inferred.
        periods = _infer_annual_periods(ts_index, fallback=annual_periods)
        # Convert annual risk_free_rate to per-bar rate for QuantStats
        rf_per_bar = risk_free_rate / periods if periods > 0 else 0.0

        sharpe = _safe_qs(qs.stats.sharpe, returns, periods=periods, rf=rf_per_bar)
        sortino = _safe_qs(qs.stats.sortino, returns, periods=periods, rf=rf_per_bar)
        calmar = _safe_qs(qs.stats.calmar, returns)
        ann_return = _safe_qs(qs.stats.cagr, returns, periods=periods)

    # Trade-level metrics from TradePnL
    net_pnls = np.array([t.net_pnl for t in trade_pnls], dtype=np.float64)
    commissions = np.array([t.commission for t in trade_pnls], dtype=np.float64)
    slippages = np.array([t.slippage for t in trade_pnls], dtype=np.float64)
    taxes = np.array([t.tax for t in trade_pnls], dtype=np.float64)

    wins = net_pnls[net_pnls > 0]
    losses_abs = np.abs(net_pnls[net_pnls < 0])
    win_rate = float(len(wins) / n_trades) if n_trades > 0 else 0.0
    # WHY: profit_factor undefined when no losses (all wins) — return None,
    # not 0.0 which misleadingly suggests worst performance.
    profit_factor = (
        float(wins.sum() / (losses_abs.sum() + EPSILON)) if len(losses_abs) > 0 else None
    )
    # WHY: payoff_ratio (avg win / avg loss) is undefined without both sides present.
    payoff_ratio = (
        float(wins.mean() / losses_abs.mean()) if len(wins) > 0 and len(losses_abs) > 0 else None
    )

    _comp = _safe_qs(qs.stats.comp, returns)
    total_ret = _comp if _comp is not None else 0.0
    # WHY: TradePnL.net_return is percentage (*100); convert to ratio
    # for consistency with other StrategyMetrics return fields.
    # Use quantity-weighted average when quantities are available
    # to correctly handle partial closes with different sizes.
    trade_returns = np.array([t.net_return for t in trade_pnls], dtype=np.float64)
    if trade_quantities is not None:
        if len(trade_quantities) != n_trades:
            raise ValueError(
                f"trade_quantities length ({len(trade_quantities)}) "
                f"must match trade_pnls length ({n_trades})"
            )
        qty_weights = np.array(trade_quantities, dtype=np.float64)
        avg_trade_return = float(np.average(trade_returns, weights=qty_weights)) / 100.0
    else:
        avg_trade_return = float(np.mean(trade_returns)) / 100.0

    exposure_ratio = 0.0
    if exposed_periods is not None and total_periods > 0:
        exposure_ratio = float(exposed_periods / total_periods)

    benchmark_return: float | None = None
    if benchmark_values and len(benchmark_values) >= 2:
        benchmark_return = float(benchmark_values[-1] / (benchmark_values[0] + EPSILON) - 1.0)

    return StrategyMetrics(
        total_return=total_ret,
        annual_return=ann_return,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=max_dd,
        trades=n_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        payoff_ratio=payoff_ratio,
        avg_trade_return=avg_trade_return,
        exposure_ratio=exposure_ratio,
        benchmark_return=benchmark_return,
        total_commission=float(commissions.sum()),
        total_slippage=float(slippages.sum()),
        total_tax=float(taxes.sum()),
    )


def _safe_qs(fn: Callable, returns: pd.Series, **kwargs: int | float) -> float | None:
    """Call a QuantStats function, return None if not computable."""
    try:
        val = fn(returns, **kwargs)
        if val is None or np.isnan(val) or np.isinf(val):
            return None
        return float(val)
    except Exception:
        logger.warning("QuantStats %s failed, returning None", fn.__name__)
        return None


def generate_tearsheet(
    equity_values: Sequence[float],
    timestamps: Sequence[datetime],
    output_path: str = "tearsheet.html",
    title: str = "Strategy Performance Report",
    benchmark_values: Sequence[float] | None = None,
) -> str:
    """Generate QuantStats HTML tearsheet report with interactive plots and tables."""
    import quantstats as qs

    if not equity_values or len(equity_values) < 2:
        logger.warning("Not enough equity curve points to generate tearsheet")
        return ""

    eq_arr = np.array(equity_values, dtype=np.float64)
    ts_index = pd.DatetimeIndex(timestamps[1:]) if len(timestamps) > 1 else pd.DatetimeIndex([])
    returns = pd.Series(
        np.diff(eq_arr) / (eq_arr[:-1] + EPSILON),
        index=ts_index,
        dtype=np.float64,
    )

    benchmark_returns = None
    if benchmark_values and len(benchmark_values) == len(equity_values):
        b_arr = np.array(benchmark_values, dtype=np.float64)
        benchmark_returns = pd.Series(
            np.diff(b_arr) / (b_arr[:-1] + EPSILON),
            index=ts_index,
            dtype=np.float64,
        )

    qs.reports.html(returns, benchmark=benchmark_returns, output=output_path, title=title)
    return output_path


def _pair_trades(
    order_events: Sequence[OrderEventRecord],
) -> list[tuple[OrderEventRecord, OrderEventRecord]]:
    """Pair each 'open' event with its following 'close' event."""
    pairs: list[tuple[OrderEventRecord, OrderEventRecord]] = []
    open_ev: OrderEventRecord | None = None
    for ev in order_events:
        if ev.event_type == "open":
            open_ev = ev
        elif ev.event_type == "close" and open_ev is not None:
            pairs.append((open_ev, ev))
            open_ev = None
    return pairs


def _strip_tz(ts: datetime) -> datetime:
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def compute_trade_mae_mfe(
    order_events: Sequence[OrderEventRecord],
    ohlcv: pd.DataFrame,
    max_periods: int | None = None,
) -> dict[str, float | list[float]]:
    """Compute MAE/MFE across executed trades.

    Args:
        order_events: Sequence of OrderEventRecord from BacktestOutput.
        ohlcv: Single-symbol OHLCV DataFrame with DatetimeIndex and 'high', 'low'.
        max_periods: If None (default), returns a single MAE/MFE percentile
            point per trade, measured over its actual open-to-close window
            (original behavior). If set, instead returns an MAE/MFE *envelope
            decay curve*: percentiles at each bar offset T=1..max_periods after
            entry, using OHLCV bars forward from entry regardless of when the
            trade actually closed — this answers "how far would price have
            moved by bar T" for stop-loss/take-profit sizing, independent of
            the strategy's own exit logic.

    Returns:
        max_periods=None: dict with n, median_mae, p75_abs_mae, median_mfe, p75_mfe (floats).
        max_periods=N: dict with n, offsets (list[int] 1..N), median_mae_curve,
            p75_mae_curve, median_mfe_curve, p75_mfe_curve (each list[float] of length N).
    """
    empty_point: dict[str, float | list[float]] = {
        "n": 0,
        "median_mae": 0.0,
        "p75_abs_mae": 0.0,
        "median_mfe": 0.0,
        "p75_mfe": 0.0,
    }
    if max_periods is not None:
        empty_point = {
            "n": 0,
            "offsets": [],
            "median_mae_curve": [],
            "p75_mae_curve": [],
            "median_mfe_curve": [],
            "p75_mfe_curve": [],
        }

    pairs = _pair_trades(order_events)
    if not pairs:
        return empty_point

    df_no_tz = ohlcv.copy()
    if df_no_tz.index.tz is not None:
        df_no_tz.index = df_no_tz.index.tz_localize(None)

    if max_periods is None:
        maes, mfes = [], []
        for entry_ev, exit_ev in pairs:
            t_entry = _strip_tz(entry_ev.ts)
            t_exit = _strip_tz(exit_ev.ts)
            w = df_no_tz.loc[(df_no_tz.index >= t_entry) & (df_no_tz.index <= t_exit)]
            if not w.empty:
                max_h = float(w["high"].max())
                min_l = float(w["low"].min())
                if entry_ev.side == "short":
                    mfe = (entry_ev.price - min_l) / entry_ev.price * 100.0
                    mae = (max_h - entry_ev.price) / entry_ev.price * 100.0
                else:
                    mfe = (max_h - entry_ev.price) / entry_ev.price * 100.0
                    mae = (entry_ev.price - min_l) / entry_ev.price * 100.0
                mfes.append(mfe)
                maes.append(mae)

        if not mfes:
            return empty_point
        arr_mae, arr_mfe = np.array(maes), np.array(mfes)
        return {
            "n": len(arr_mae),
            "median_mae": float(np.median(arr_mae)),
            "p75_abs_mae": float(np.percentile(np.abs(arr_mae), 75)),
            "median_mfe": float(np.median(arr_mfe)),
            "p75_mfe": float(np.percentile(arr_mfe, 75)),
        }

    # Envelope decay curve: per-trade running MAE/MFE at each bar offset,
    # padded with NaN once a trade runs out of forward bars, then aggregated
    # column-wise with nan-aware percentiles so short-lived trades don't skew
    # later offsets.
    mfe_rows, mae_rows = [], []
    for entry_ev, _exit_ev in pairs:
        t_entry = _strip_tz(entry_ev.ts)
        # WHY +1: offset T=1 means "1 bar after entry", not the entry bar itself.
        start_pos = df_no_tz.index.searchsorted(t_entry, side="left") + 1
        window = df_no_tz.iloc[start_pos : start_pos + max_periods]
        if window.empty:
            continue
        running_max_h = np.maximum.accumulate(window["high"].to_numpy(dtype=np.float64))
        running_min_l = np.minimum.accumulate(window["low"].to_numpy(dtype=np.float64))
        if entry_ev.side == "short":
            mfe_curve = (entry_ev.price - running_min_l) / entry_ev.price * 100.0
            mae_curve = (running_max_h - entry_ev.price) / entry_ev.price * 100.0
        else:
            mfe_curve = (running_max_h - entry_ev.price) / entry_ev.price * 100.0
            mae_curve = (entry_ev.price - running_min_l) / entry_ev.price * 100.0
        pad = max_periods - len(mfe_curve)
        if pad > 0:
            mfe_curve = np.concatenate([mfe_curve, np.full(pad, np.nan)])
            mae_curve = np.concatenate([mae_curve, np.full(pad, np.nan)])
        mfe_rows.append(mfe_curve)
        mae_rows.append(mae_curve)

    if not mfe_rows:
        return empty_point

    mfe_mat = np.vstack(mfe_rows)
    mae_mat = np.vstack(mae_rows)
    with np.errstate(all="ignore"):
        median_mfe_curve = np.nanmedian(mfe_mat, axis=0)
        p75_mfe_curve = np.nanpercentile(mfe_mat, 75, axis=0)
        median_mae_curve = np.nanmedian(mae_mat, axis=0)
        p75_mae_curve = np.nanpercentile(np.abs(mae_mat), 75, axis=0)

    return {
        "n": len(mfe_rows),
        "offsets": list(range(1, max_periods + 1)),
        "median_mae_curve": [float(v) for v in median_mae_curve],
        "p75_mae_curve": [float(v) for v in p75_mae_curve],
        "median_mfe_curve": [float(v) for v in median_mfe_curve],
        "p75_mfe_curve": [float(v) for v in p75_mfe_curve],
    }


def compute_trade_durations(order_events: Sequence[OrderEventRecord]) -> list[int]:
    """Extract holding-period durations (in bars) for each closed trade.

    Reads the already-computed ``periods_held`` field on 'close' events —
    no recomputation, just aggregation for a duration-distribution chart.
    """
    return [
        ev.periods_held
        for ev in order_events
        if ev.event_type == "close" and ev.periods_held is not None
    ]


def compute_pnl_by_trade(order_events: Sequence[OrderEventRecord]) -> list[float]:
    """Cumulative PnL ordered by trade sequence (not calendar time).

    Useful for event-driven strategies where a calendar-time equity curve
    is mostly flat between infrequent trades — this compresses it to one
    point per closed trade.
    """
    closes = sorted(
        (ev for ev in order_events if ev.event_type == "close"),
        key=lambda ev: ev.ts,
    )
    cumulative = np.cumsum([ev.pnl or 0.0 for ev in closes])
    return [float(v) for v in cumulative]


# WHY these two colors specifically: matches the green=favorable/red=adverse
# threshold-coloring convention already used in the Grafana strategy_dashboard
# (app/grafana/generate_dashboards.py) — local HTML reports and Grafana panels
# read as the same visual system instead of picking an arbitrary new palette.
_FAVORABLE = "#2a9d8f"
_ADVERSE = "#e63946"
_NEUTRAL = "#3d5a80"
_MUTED = "#8a8f98"


def _style_axes(ax: Axes) -> None:
    """Shared minimal chart style: no chartjunk, readable on light or dark cards.

    Colors are mid-grey (not pure black) and the figure is saved transparent
    (see generate_trade_tearsheet) so charts sit directly on the card
    background in both light and dark mode without a baked-in white box.
    """
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(_MUTED)
    ax.grid(True, color=_MUTED, linewidth=0.5, alpha=0.25)
    ax.set_axisbelow(True)
    ax.tick_params(colors=_MUTED, labelsize=9)
    ax.title.set_color("#555555")
    ax.title.set_fontsize(11)
    ax.xaxis.label.set_color(_MUTED)
    ax.yaxis.label.set_color(_MUTED)


def _plot_trade_durations(ax: Axes, durations: list[int]) -> None:
    if durations:
        ax.hist(
            durations,
            bins=min(30, max(1, len(set(durations)))),
            color=_NEUTRAL,
            edgecolor="white",
            linewidth=0.5,
        )
    _style_axes(ax)
    ax.set_title("Trade Duration Distribution")
    ax.set_xlabel("Periods held (bars)")
    ax.set_ylabel("Trade count")


def _plot_pnl_by_trade(ax: Axes, pnl_curve: list[float]) -> None:
    x = list(range(1, len(pnl_curve) + 1))
    if pnl_curve:
        arr = np.array(pnl_curve)
        ax.fill_between(x, arr, 0, where=arr >= 0, color=_FAVORABLE, alpha=0.15, linewidth=0)
        ax.fill_between(x, arr, 0, where=arr < 0, color=_ADVERSE, alpha=0.15, linewidth=0)
        ax.plot(x, arr, color=_NEUTRAL, linewidth=1.6, zorder=3)
    ax.axhline(0, color=_MUTED, linewidth=0.7)
    _style_axes(ax)
    ax.set_title("PnL by Trade (sequential, not calendar time)")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Cumulative PnL")


def _plot_mae_mfe_envelope(ax: Axes, envelope: dict[str, float | list[float]]) -> None:
    offsets = envelope.get("offsets", [])
    if offsets:
        ax.plot(
            offsets,
            envelope["median_mfe_curve"],
            color=_FAVORABLE,
            linewidth=1.8,
            label="median MFE",
        )
        ax.plot(
            offsets,
            envelope["p75_mfe_curve"],
            color=_FAVORABLE,
            linewidth=1.1,
            linestyle="--",
            label="p75 MFE",
        )
        ax.plot(
            offsets, envelope["median_mae_curve"], color=_ADVERSE, linewidth=1.8, label="median MAE"
        )
        ax.plot(
            offsets,
            envelope["p75_mae_curve"],
            color=_ADVERSE,
            linewidth=1.1,
            linestyle="--",
            label="p75 MAE",
        )
        ax.legend(frameon=False, fontsize=8.5, labelcolor=_MUTED)
    _style_axes(ax)
    ax.set_title(f"MAE/MFE Envelope Decay (n={envelope.get('n', 0)} trades)")
    ax.set_xlabel("Bars after entry")
    ax.set_ylabel("% move from entry price")


def _fmt_stat(value: float | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


_TEARSHEET_CSS = """
:root { --bg:#fafafa; --card:#ffffff; --text:#1a1a1a; --muted:#6b7280; --border:#e5e7eb; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15181c; --card:#1e2227; --text:#e8e8e8; --muted:#9aa0a6; --border:#2c3138; }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text);
  max-width: 880px; margin: 0 auto; padding: 2.5rem 1.5rem 4rem;
}
h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.2rem; }
.subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 1.75rem; }
.tiles { display: flex; gap: 0.75rem; margin-bottom: 2rem; flex-wrap: wrap; }
.tile {
  flex: 1; min-width: 130px; background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 0.9rem 1.1rem;
}
.tile-value { font-size: 1.35rem; font-weight: 600; }
.tile-label {
  font-size: 0.7rem; color: var(--muted); margin-top: 0.25rem;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 0.75rem; margin-bottom: 1.25rem;
}
.card img { width: 100%; display: block; }
"""


def generate_trade_tearsheet(
    output: BacktestOutput,
    ohlcv: pd.DataFrame,
    output_path: str = "trade_tearsheet.html",
    max_periods: int = 48,
) -> str:
    """Generate an HTML trade-level tearsheet for event-driven/high-frequency strategies.

    Complements ``generate_tearsheet()`` (equity-based, QuantStats, industry
    standard for calendar-rebalanced strategies) with trade-based views —
    duration distribution, PnL-by-trade, and MAE/MFE envelope decay — for
    strategies that trade infrequently or hold for hours rather than months,
    where a monthly calendar heatmap carries no information. See the
    trade-based vs equity-based split in
    docs/decisions/2026-03-26-performance-metrics-standard.md.

    Adding a new trade-metric dimension (e.g. R-multiple distribution, win/
    loss streaks): write one ``_plot_*(ax, data)`` function above and append
    one entry to the ``charts`` list below — no other assembly code changes.
    """
    import base64
    from io import BytesIO

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    order_events = output.order_events
    durations = compute_trade_durations(order_events)
    pnl_curve = compute_pnl_by_trade(order_events)
    envelope = compute_trade_mae_mfe(order_events, ohlcv, max_periods=max_periods)

    charts: list[Callable[[Axes], None]] = [
        lambda ax: _plot_trade_durations(ax, durations),
        lambda ax: _plot_pnl_by_trade(ax, pnl_curve),
        lambda ax: _plot_mae_mfe_envelope(ax, envelope),
    ]

    def _fig_to_base64(fig: Any) -> str:
        buf = BytesIO()
        # WHY transparent=True: lets each chart sit on the card background
        # (light or dark) instead of a baked-in white box — see _style_axes.
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=130, transparent=True)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    cards = []
    for builder in charts:
        fig, ax = plt.subplots(figsize=(7.6, 3.1))
        builder(ax)
        cards.append(
            f'<div class="card"><img src="data:image/png;base64,{_fig_to_base64(fig)}"/></div>'
        )

    m = output.metrics
    tiles = "".join(
        f'<div class="tile"><div class="tile-value">{_fmt_stat(value)}</div>'
        f'<div class="tile-label">{label}</div></div>'
        for label, value in [
            ("Trades", m.trades),
            ("Win rate", m.win_rate),
            ("Profit factor", m.profit_factor),
            ("Payoff ratio", m.payoff_ratio),
        ]
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Trade Tearsheet — {output.run_metadata.run_id}</title>
<style>{_TEARSHEET_CSS}</style>
</head>
<body>
<h1>Trade Tearsheet</h1>
<div class="subtitle">{output.run_metadata.strategy} · {output.run_metadata.symbol} · {output.run_metadata.timeframe}</div>
<div class="tiles">{tiles}</div>
{"".join(cards)}
</body></html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path
