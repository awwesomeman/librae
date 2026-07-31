"""Smoke the installed Librae distribution outside the repository checkout."""

from __future__ import annotations

import argparse
import os
import sys
from importlib.metadata import entry_points, version
from importlib.resources import files
from pathlib import Path

import librae
import pandas as pd
from librae import Backtest, Context, OrderIntent, Strategy


class BuyOnce(Strategy):
    """Open one position and let terminal liquidation close it."""

    def on_bar(self, context: Context) -> list[OrderIntent]:
        if context.period_index == 0:
            return [OrderIntent(action="long", symbol=context.symbol, quantity=1.0)]
        return []


def _validate_install_location(*, expect_editable: bool) -> None:
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace is None:
        return

    module_path = Path(librae.__file__).resolve()
    checkout_path = Path(workspace).resolve()
    imported_from_checkout = module_path.is_relative_to(checkout_path)
    assert imported_from_checkout is expect_editable, (
        f"expected editable={expect_editable}, imported {module_path}"
    )


def _validate_metadata_and_package_data(*, expect_editable: bool) -> None:
    distribution_version = version("librae")
    if not expect_editable:
        assert librae.__version__ == distribution_version
    assert librae.__version__ != "unknown"
    assert distribution_version != "unknown"

    console_scripts = {
        entry_point.name: entry_point for entry_point in entry_points(group="console_scripts")
    }
    assert console_scripts["librae"].value == "librae._scaffold:main"

    package_root = files("librae")
    assert (package_root / "_scaffold" / "env.example").is_file()
    assert (package_root / "db" / "timescale_init.sql").is_file()
    assert (
        package_root
        / "app"
        / "grafana"
        / "provisioning"
        / "dashboards"
        / "json"
        / "strategy_dashboard.json"
    ).is_file()


def _validate_console_entry_point() -> None:
    entry_point = next(
        item for item in entry_points(group="console_scripts") if item.name == "librae"
    )
    command = entry_point.load()
    original_argv = sys.argv
    try:
        sys.argv = ["librae", "init"]
        command()
    finally:
        sys.argv = original_argv
    assert Path(".env.example").is_file()


def _validate_minimal_backtest() -> None:
    timestamps = pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")
    index = pd.MultiIndex.from_arrays(
        [["BTCUSDT"] * len(timestamps), timestamps],
        names=["symbol", "datetime"],
    )
    close = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0], index=index)
    data = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000.0,
        },
        index=index,
    )

    backtest = Backtest(
        data=data,
        strategy=BuyOnce(),
        initial_balance=10_000.0,
        data_source="synthetic",
    )
    backtest.run()
    output = backtest.build_output()

    assert output.run_metadata.symbols == ("BTCUSDT",)
    assert len(output.equity_curve) == len(timestamps)
    assert output.metrics.trades == 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-editable", action="store_true")
    args = parser.parse_args()

    _validate_install_location(expect_editable=args.expect_editable)
    _validate_metadata_and_package_data(expect_editable=args.expect_editable)
    _validate_console_entry_point()
    _validate_minimal_backtest()
    print(f"Installed package smoke passed: {librae.__version__} ({librae.__file__})")


if __name__ == "__main__":
    main()
