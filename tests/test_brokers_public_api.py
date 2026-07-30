"""Public broker imports stay explicit and SDK loading remains lazy."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_public_broker_api_exports_adapters_and_credentials():
    repository_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from brokers import ("
                "BinanceStocksAdapter, BinanceStocksCredentials, CredentialConfig, "
                "CryptoAdapter, CryptoCredentials, IBKRAdapter, IBKRCredentials, "
                "ShioajiAdapter, ShioajiCredentials"
                "); "
                "assert all(issubclass(item, CredentialConfig) for item in ("
                "BinanceStocksCredentials, CryptoCredentials, IBKRCredentials, "
                "ShioajiCredentials"
                ")); "
                "assert all(item is not None for item in ("
                "BinanceStocksAdapter, CryptoAdapter, IBKRAdapter, ShioajiAdapter"
                "))"
            ),
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
