"""Format-neutral tabular artifacts for caller-owned local persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from librae.backtest.schema import (
    AllocationSnapshotPoint,
    EquityCurvePoint,
    FundingCashFlowRecord,
    OrderEventRecord,
    PositionSnapshotPoint,
    StrategyMetrics,
)
from librae.core.market_data import validate_ohlcv_values

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from librae.backtest.schema import BacktestOutput

ARTIFACT_SCHEMA_VERSION = 1
ArtifactKind = Literal["market_data", "backtest_output"]


@dataclass(frozen=True)
class TabularArtifact:
    """Versioned metadata and logical tables ready for caller-selected storage."""

    manifest: Mapping[str, Any]
    tables: Mapping[str, pd.DataFrame]


def _package_version() -> str:
    try:
        return version("librae")
    except PackageNotFoundError:
        return "0.0.0.dev0+unknown"


def _manifest(kind: ArtifactKind, **metadata: object) -> dict[str, Any]:
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": kind,
        "librae_version": _package_version(),
        "created_at": datetime.now(UTC).isoformat(),
        **metadata,
    }


def _normalized_market_data(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    validate_ohlcv_values(frame, context="market-data artifact")

    if "ts" in frame.columns:
        table = frame.reset_index(drop=True).copy()
    elif isinstance(frame.index, pd.MultiIndex):
        timestamp_level = next(
            (name for name in ("datetime", "ts") if name in frame.index.names),
            None,
        )
        if timestamp_level is None:
            raise ValueError("market data requires a ts column or datetime/ts index level")
        table = frame.reset_index()
        table = table.rename(columns={timestamp_level: "ts"})
    elif isinstance(frame.index, pd.DatetimeIndex):
        timestamp_name = frame.index.name or "index"
        table = frame.reset_index().rename(columns={timestamp_name: "ts"})
    else:
        raise ValueError("market data requires a ts column or datetime/ts index")

    timestamps = pd.DatetimeIndex(table["ts"])
    if timestamps.tz is None:
        raise ValueError("market-data timestamps must be timezone-aware")
    if timestamps.hasnans:
        raise ValueError("market-data timestamps must not contain null values")
    table["ts"] = timestamps.tz_convert(UTC)

    if "symbol" in table.columns:
        observed = set(table["symbol"].dropna().astype(str))
        if observed != {symbol}:
            raise ValueError(
                f"market-data symbol column must contain only {symbol!r}, got {sorted(observed)!r}"
            )
    else:
        table.insert(0, "symbol", symbol)
    return table


def _validate_identity(identity: Mapping[str, str]) -> None:
    invalid = [
        name for name, value in identity.items() if not isinstance(value, str) or not value.strip()
    ]
    if invalid:
        raise ValueError(f"market-data identity fields must be non-empty strings: {invalid}")


def build_market_data_artifact(
    frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    data_source: str,
    instrument_type: str,
) -> TabularArtifact:
    """Build one enriched OHLCV table without selecting or writing a file format."""
    identity = {
        "symbol": symbol,
        "timeframe": timeframe,
        "data_source": data_source,
        "instrument_type": instrument_type,
    }
    _validate_identity(identity)
    table = _normalized_market_data(frame, symbol=symbol)

    for name, expected in identity.items():
        if name in table.columns:
            observed = set(table[name].dropna().astype(str))
            if table[name].isna().any() or observed != {expected}:
                raise ValueError(
                    f"market-data {name} column must contain only {expected!r}, "
                    f"got {sorted(observed)!r}"
                )
        else:
            table[name] = expected

    return TabularArtifact(
        manifest=_manifest("market_data", **identity),
        tables={"market_data": table},
    )


def _records_frame(
    records: Sequence[object],
    record_type: type,
    *,
    run_id: str,
) -> pd.DataFrame:
    columns = ["run_id", *(field.name for field in fields(record_type))]
    return pd.DataFrame(
        [{"run_id": run_id, **asdict(record)} for record in records],
        columns=columns,
    )


def build_backtest_artifact(
    output: BacktestOutput,
    *,
    config_hash: str,
) -> TabularArtifact:
    """Flatten a completed ``BacktestOutput`` into stable logical tables."""
    if not isinstance(config_hash, str) or not config_hash.strip():
        raise ValueError("config_hash must be a non-empty string")
    output.validate()
    run_id = output.run_metadata.run_id

    account = output.account
    account_rows = [
        {
            "run_id": run_id,
            "account_id": account.account_id,
            "currency": account.currency,
            "initial_cash": account.initial_cash,
            "final_equity": account.final_equity,
            "net_pnl": account.net_pnl,
            **asdict(account.metrics),
        }
    ]
    equity_rows = [
        {
            "run_id": run_id,
            "account_id": account.account_id,
            "currency": account.currency,
            **asdict(point),
        }
        for point in account.equity_curve
    ]

    tables = {
        "accounts": pd.DataFrame(
            account_rows,
            columns=[
                "run_id",
                "account_id",
                "currency",
                "initial_cash",
                "final_equity",
                "net_pnl",
                *(field.name for field in fields(StrategyMetrics)),
            ],
        ),
        "equity_curve": pd.DataFrame(
            equity_rows,
            columns=[
                "run_id",
                "account_id",
                "currency",
                *(field.name for field in fields(EquityCurvePoint)),
            ],
        ),
        "order_events": _records_frame(
            output.order_events,
            OrderEventRecord,
            run_id=run_id,
        ),
        "position_snapshots": _records_frame(
            output.position_snapshots,
            PositionSnapshotPoint,
            run_id=run_id,
        ),
        "allocation_snapshots": _records_frame(
            output.allocation_snapshots,
            AllocationSnapshotPoint,
            run_id=run_id,
        ),
        "funding_cash_flows": _records_frame(
            output.funding_cash_flows,
            FundingCashFlowRecord,
            run_id=run_id,
        ),
    }
    return TabularArtifact(
        manifest=_manifest(
            "backtest_output",
            config_hash=config_hash,
            run_metadata=output.to_dict()["run_metadata"],
        ),
        tables=tables,
    )
