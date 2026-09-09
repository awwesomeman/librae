"""Same-symbol multi-frequency market data in live and simulation.

A run has exactly one executing cadence per symbol — two primaries for one
position would mean two execution cadences for one book, which backtest
already refuses ("primary_subscriptions must contain one identity per
symbol"). Extra frequencies are therefore auxiliary: read-only context the
strategy reads through ``ctx.market_data``, never a source of fills.

Backtest has supported this since the mixed-frequency replay work; these
tests are about closing the gap in live, where a strategy previously had to
resample inside itself and pay warmup for the finer bars.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.core.run_config import AccountConfig, AuxiliarySubscription, RunConfig


def _config(**overrides) -> RunConfig:
    base = {
        "strategy_name": "t",
        "mode": "backtest",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "timeframe": "H1",
        "market": "crypto",
        "data_source": "binance_spot",
        "account": AccountConfig(currency="USDT", initial_cash=100_000.0),
    }
    return RunConfig(**{**base, **overrides})


class TestAuxiliaryDeclaration:
    def test_none_by_default(self) -> None:
        assert _config().auxiliary_subscriptions == ()

    def test_a_coarser_timeframe_for_a_configured_symbol_is_accepted(self) -> None:
        config = _config(
            auxiliary_subscriptions=[AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1")]
        )

        assert config.auxiliary_subscriptions[0].timeframe == "D1"

    def test_the_symbol_must_already_be_in_the_run(self) -> None:
        """An auxiliary reuses its symbol's resolved instrument and data
        route, so it cannot introduce an instrument the run never resolved."""
        with pytest.raises(ValueError, match="configured symbols"):
            _config(auxiliary_subscriptions=[AuxiliarySubscription(symbol="SPY", timeframe="D1")])

    def test_it_cannot_duplicate_the_primary_cadence(self) -> None:
        """Same symbol, same timeframe, same session mode is the primary."""
        with pytest.raises(ValueError, match="primary"):
            _config(
                auxiliary_subscriptions=[AuxiliarySubscription(symbol="BTCUSDT", timeframe="H1")]
            )

    def test_it_cannot_duplicate_another_auxiliary(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            _config(
                auxiliary_subscriptions=[
                    AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),
                    AuxiliarySubscription(symbol="BTCUSDT", timeframe="1d"),
                ]
            )

    def test_the_timeframe_is_canonicalized(self) -> None:
        config = _config(
            auxiliary_subscriptions=[AuxiliarySubscription(symbol="BTCUSDT", timeframe="1d")]
        )

        assert config.auxiliary_subscriptions[0].timeframe == "D1"

    def test_auxiliary_inputs_change_the_config_hash(self) -> None:
        """The strategy sees different data, so it is a different run."""
        with_aux = _config(
            auxiliary_subscriptions=[AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1")]
        )

        assert _config().config_hash != with_aux.config_hash

    def test_declaration_order_does_not_change_the_config_hash(self) -> None:
        forward = _config(
            auxiliary_subscriptions=[
                AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),
                AuxiliarySubscription(symbol="ETHUSDT", timeframe="D1"),
            ]
        )
        reversed_ = _config(
            auxiliary_subscriptions=[
                AuxiliarySubscription(symbol="ETHUSDT", timeframe="D1"),
                AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),
            ]
        )

        assert forward.config_hash == reversed_.config_hash


class TestLiveExposesAuxiliaryHistory:
    """The gap this closes: research could subscribe a symbol at two
    frequencies, live could not. A strategy needing daily context while
    executing hourly had to resample inside itself and warm up the finer
    bars to cover it."""

    @staticmethod
    def _bars(n: int, freq: str, start: str):
        import pandas as pd

        return pd.DataFrame(
            {
                "ts": pd.date_range(start, periods=n, freq=freq, tz="UTC"),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [1_000.0] * n,
            }
        )

    def _run(self, auxiliary: list[AuxiliarySubscription]):
        from librae.core.strategy import Strategy

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        seen: list[tuple] = []

        class RecordSubscriptions(Strategy):
            def on_bar(self, ctx):
                seen.append(
                    ()
                    if ctx.market_data is None
                    else tuple(
                        (s.symbol, s.timeframe, len(ctx.market_data.history(s)))
                        for s in ctx.market_data.subscriptions
                    )
                )
                return []

        def fetch(_symbol, timeframe, _limit, *, drop_incomplete=False):
            del drop_incomplete
            # The daily series completes before the hourly frontier reaches
            # 2025-01-01; a daily bar opening that morning would not yet be
            # available, which is the availability contract working, not a gap.
            if timeframe == "D1":
                return self._bars(6, "D", "2024-12-25T00:00Z")
            return self._bars(6, "h", "2025-01-01T00:00Z")

        runner = TestLiveTrader()._make_runner(
            strategy=RecordSubscriptions(),
            fetcher=fetch,
            config=_test_cfg(
                mode="sim",
                warmup_periods=1,
                auxiliary_subscriptions=tuple(auxiliary),
            ),
        )
        runner._poll_cycle()
        return seen

    def test_without_a_declaration_only_the_executing_cadence_is_visible(self) -> None:
        seen = self._run([])

        assert seen
        assert all(timeframe == "H1" for entry in seen for _, timeframe, _ in entry)

    def test_a_declared_daily_input_reaches_the_strategy(self) -> None:
        seen = self._run([AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1")])

        assert seen
        frequencies = {timeframe for entry in seen for _, timeframe, _ in entry}
        assert frequencies == {"H1", "D1"}

    def test_the_two_frequencies_keep_separate_history(self) -> None:
        """Same symbol, two identities: their caches must not collide."""
        seen = self._run([AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1")])

        by_timeframe = {timeframe: rows for entry in seen for _, timeframe, rows in entry}
        assert by_timeframe["D1"] > 0
        assert by_timeframe["H1"] > 0


def test_the_strategy_config_file_reaches_run_config(tmp_path, monkeypatch) -> None:
    """A documented YAML key build_run never reads is silently dropped, with
    no error and an unchanged config_hash — the bug calendar_id had in #210
    and optional_symbols had in #218."""
    import sys
    import textwrap

    from librae.orchestration.cli import build_run

    (tmp_path / "config.yaml").write_text(
        textwrap.dedent(
            """\
            strategy:
              symbol: BTCUSDT
              timeframe: 1h
              auxiliary_subscriptions:
                - symbol: BTCUSDT
                  timeframe: 1d
            """
        )
    )
    monkeypatch.setattr(sys, "argv", ["test"])

    config, _ = build_run("test_strat", str(tmp_path / "run.py"))

    assert config.auxiliary_subscriptions == (
        AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),
    )


class TestBacktestLiveParity:
    """Backtest is handed its auxiliary frames; live fetches them. The same
    RunConfig must not mean different things in the two modes."""

    @staticmethod
    def _panel():
        import pandas as pd

        index = pd.MultiIndex.from_product(
            [["BTCUSDT"], pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")],
            names=["symbol", "datetime"],
        )
        return pd.DataFrame(
            {
                "open": [100.0] * 6,
                "high": [101.0] * 6,
                "low": [99.0] * 6,
                "close": [100.0] * 6,
                "volume": [1_000.0] * 6,
            },
            index=index,
        )

    @staticmethod
    def _config(auxiliary):
        return RunConfig(
            strategy_name="t",
            mode="backtest",
            symbols=["BTCUSDT"],
            timeframe="H1",
            market="crypto",
            data_source="binance_spot",
            account=AccountConfig(currency="USDT", initial_cash=100_000.0),
            auxiliary_subscriptions=auxiliary,
        )

    def _build(self, auxiliary, auxiliary_data):
        from librae.backtest.engine import Backtest
        from librae.core.cost_model import CostModel
        from librae.core.strategy import Strategy

        class Hold(Strategy):
            def on_bar(self, ctx):
                return []

        return Backtest(
            self._panel(),
            Hold(),
            config=self._config(auxiliary),
            cost_model=CostModel.zero(),
            auxiliary_data=auxiliary_data,
        )

    def test_a_declared_auxiliary_must_be_supplied(self) -> None:
        with pytest.raises(ValueError, match="auxiliary_data must supply"):
            self._build(
                (AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),),
                auxiliary_data=None,
            )

    def test_supplying_the_declared_identity_is_accepted(self) -> None:
        import pandas as pd
        from librae.core.market_data import MarketDataSubscription

        subscription = MarketDataSubscription(
            symbol="BTCUSDT",
            timeframe="D1",
            calendar_id="24/7",
            session_mode="extended",
            data_source="binance_spot",
            instrument_type="spot",
        )
        frame = pd.DataFrame(
            {
                "ts": pd.date_range("2024-12-25", periods=3, freq="D", tz="UTC"),
                "open": [100.0] * 3,
                "high": [101.0] * 3,
                "low": [99.0] * 3,
                "close": [100.0] * 3,
                "volume": [1_000.0] * 3,
            }
        ).set_index("ts")

        self._build(
            (AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),),
            auxiliary_data={subscription: frame},
        )

    def test_undeclared_frames_stay_supported(self) -> None:
        """The Python-API mixed-frequency path predates the declaration and
        never claimed live parity, so passing frames without declaring them
        must keep working."""
        import pandas as pd
        from librae.core.market_data import MarketDataSubscription

        subscription = MarketDataSubscription(
            symbol="BTCUSDT",
            timeframe="D1",
            calendar_id="24/7",
            session_mode="extended",
            data_source="binance_spot",
            instrument_type="spot",
        )
        frame = pd.DataFrame(
            {
                "ts": pd.date_range("2024-12-25", periods=3, freq="D", tz="UTC"),
                "open": [100.0] * 3,
                "high": [101.0] * 3,
                "low": [99.0] * 3,
                "close": [100.0] * 3,
                "volume": [1_000.0] * 3,
            }
        ).set_index("ts")

        self._build((), auxiliary_data={subscription: frame})


class TestAuxiliaryCannotBreakTheCycle:
    """An auxiliary is read-only context. Nothing about its health may stop
    the primary from executing, and its declared identity must stay visible
    to the strategy whatever the feed does."""

    @staticmethod
    def _bars(n: int, freq: str, start: str):
        import pandas as pd

        return pd.DataFrame(
            {
                "ts": pd.date_range(start, periods=n, freq=freq, tz="UTC"),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [1_000.0] * n,
            }
        )

    def _run(self, auxiliary_fetch, *, symbols=("BTCUSDT",), optional=()):
        from librae.core.strategy import Strategy

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        seen: list[tuple] = []

        class ReadAuxiliary(Strategy):
            def on_bar(self, ctx):
                if ctx.market_data is not None:
                    seen.append(
                        tuple(
                            (s.timeframe, len(ctx.market_data.history(s)))
                            for s in ctx.market_data.subscriptions
                        )
                    )
                return []

        def fetch(symbol, timeframe, _limit, *, drop_incomplete=False):
            del drop_incomplete
            if timeframe == "D1":
                return auxiliary_fetch()
            if symbol in optional:
                # An optional symbol that never delivers: the #218 scenario,
                # and the one that reached a batch-only precondition.
                return self._bars(0, "h", "2025-01-01T00:00Z")
            return self._bars(6, "h", "2025-01-01T00:00Z")

        runner = TestLiveTrader()._make_runner(
            strategy=ReadAuxiliary(),
            fetcher=fetch,
            config=_test_cfg(
                mode="sim",
                symbols=list(symbols),
                warmup_periods=1,
                optional_symbols=optional,
                auxiliary_subscriptions=(AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),),
            ),
        )
        runner._poll_cycle()
        return seen

    def test_a_raising_auxiliary_feed_does_not_stop_the_primary(self) -> None:
        def explode():
            raise RuntimeError("auxiliary feed unavailable")

        assert self._run(explode), "the primary cadence must still evaluate"

    def test_unusable_auxiliary_rows_do_not_stop_the_primary(self) -> None:
        """Normalization runs on auxiliary data too; a duplicate timestamp
        used to escape the poll cycle and skip the primary's execution."""
        import pandas as pd

        def duplicated():
            frame = self._bars(2, "D", "2024-12-25T00:00Z")
            return pd.concat([frame, frame], ignore_index=True)

        assert self._run(duplicated)

    def test_an_absent_optional_symbol_does_not_import_batch_preconditions(self) -> None:
        """ctx.market_data is built for plain feature_fn strategies too, but it
        must not carry batch-only requirements: a symbol with no history is a
        readiness question the gate already answered, not an error here."""
        seen = self._run(
            lambda: self._bars(6, "D", "2024-12-25T00:00Z"),
            symbols=("BTCUSDT", "ETHUSDT"),
            optional=("ETHUSDT",),
        )

        assert seen, "the primary cadence must still evaluate"

    def test_a_declared_identity_stays_visible_when_its_feed_is_down(self) -> None:
        """ctx.market_data.history() raises KeyError for an unknown identity,
        so the subscription set must not change shape with feed health."""

        def explode():
            raise RuntimeError("auxiliary feed unavailable")

        seen = self._run(explode)

        assert any(timeframe == "D1" for entry in seen for timeframe, _ in entry)


class TestStalledAuxiliaryIsReported:
    """A dead auxiliary must be reported however it died.

    Evaluating freshness only after a successful fetch checks the feed exactly
    when it is healthy enough to answer, and never when it is not, so the two
    ordinary ways a feed dies -- raising and returning nothing -- stay silent
    while the cached frame ages and the strategy keeps reading it as context.
    """

    # Phase 1 sees a healthy, current auxiliary; phase 2 moves the clock past
    # its due date. The cache only ever goes stale by ageing, never by being
    # fetched stale -- otherwise the successful fetch would raise the alert and
    # the test would pass without evaluating anything on the failure path.
    FRESH_CLOCK = datetime(2025, 1, 4, tzinfo=UTC)
    LATER_CLOCK = datetime(2025, 1, 10, tzinfo=UTC)

    @staticmethod
    def _bars(n: int, freq: str, start: str):
        import pandas as pd

        return pd.DataFrame(
            {
                "ts": pd.date_range(start, periods=n, freq=freq, tz="UTC"),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [1_000.0] * n,
            }
        )

    @staticmethod
    def _bars_ending(n: int, freq: str, end):
        """Completed bars up to the clock, so the primary stays warm and fresh."""
        import pandas as pd

        return pd.DataFrame(
            {
                "ts": pd.date_range(end=end, periods=n, freq=freq, tz="UTC"),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [1_000.0] * n,
            }
        )

    def _alerts_after(self, failed_auxiliary_fetch, *, later_cycles: int = 2) -> list[dict]:
        """Warm the auxiliary cache while healthy, then break the feed."""
        from librae.core.strategy import Strategy

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        clock = {"now": self.FRESH_CLOCK}
        warmed = {"done": False}
        evaluated: list[int] = []

        class Hold(Strategy):
            def on_bar(self, ctx):
                evaluated.append(1)
                return []

        def fetch(_symbol, timeframe, _limit, *, drop_incomplete=False):
            del drop_incomplete
            if timeframe == "D1":
                if warmed["done"]:
                    return failed_auxiliary_fetch()
                # Current as of FRESH_CLOCK, so no alert can fire here.
                return self._bars(3, "D", "2025-01-01T00:00Z")
            return self._bars_ending(6, "h", clock["now"])

        alerts: list[dict] = []
        runner = TestLiveTrader()._make_runner(
            strategy=Hold(),
            fetcher=fetch,
            config=_test_cfg(
                mode="sim",
                warmup_periods=1,
                auxiliary_subscriptions=(AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),),
            ),
            clock=lambda: clock["now"],
        )
        runner.STALE_DATA_TOLERANCE_BARS = 2
        runner._notify = lambda method, **kwargs: alerts.append(kwargs)

        runner._poll_cycle()
        assert not [a for a in alerts if "Stale Auxiliary" in a.get("title", "")], (
            "the warm-up cycle must not alert, or the test proves nothing"
        )

        warmed["done"] = True
        clock["now"] = self.LATER_CLOCK
        for _ in range(later_cycles):
            runner._poll_cycle()

        assert evaluated, "a stalled auxiliary must not hold the primary cadence"
        return [a for a in alerts if "Stale Auxiliary Data" in a.get("title", "")]

    def test_a_raising_feed_still_reports_the_ageing_cache(self) -> None:
        """The cache goes on ageing while the strategy reads it as context."""

        def raises():
            raise RuntimeError("auxiliary feed unavailable")

        assert len(self._alerts_after(raises)) == 1

    def test_an_empty_feed_still_reports_the_ageing_cache(self) -> None:
        def empty():
            return self._bars(0, "D", "2025-01-01T00:00Z")

        assert len(self._alerts_after(empty)) == 1

    def test_a_feed_that_keeps_answering_with_stale_rows_is_reported(self) -> None:
        def stale_rows():
            return self._bars(3, "D", "2025-01-01T00:00Z")

        assert len(self._alerts_after(stale_rows)) == 1

    def test_the_alert_is_edge_triggered_across_cycles(self) -> None:
        def raises():
            raise RuntimeError("auxiliary feed unavailable")

        assert len(self._alerts_after(raises, later_cycles=4)) == 1

    def test_a_feed_that_never_delivered_does_not_alert(self) -> None:
        """Never-arrived is not arrived-and-stopped: there is no observation to
        be late relative to, so it is logged rather than alerted."""
        from librae.core.strategy import Strategy

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        class Hold(Strategy):
            def on_bar(self, ctx):
                return []

        def fetch(_symbol, timeframe, _limit, *, drop_incomplete=False):
            del drop_incomplete
            if timeframe == "D1":
                raise RuntimeError("auxiliary feed never available")
            return self._bars(6, "h", "2025-01-09T18:00Z")

        alerts: list[dict] = []
        runner = TestLiveTrader()._make_runner(
            strategy=Hold(),
            fetcher=fetch,
            config=_test_cfg(
                mode="sim",
                warmup_periods=1,
                auxiliary_subscriptions=(AuxiliarySubscription(symbol="BTCUSDT", timeframe="D1"),),
            ),
            clock=lambda: self.LATER_CLOCK,
        )
        runner.STALE_DATA_TOLERANCE_BARS = 2
        runner._notify = lambda method, **kwargs: alerts.append(kwargs)

        for _ in range(3):
            runner._poll_cycle()

        assert not [a for a in alerts if "Stale Auxiliary" in a.get("title", "")]


def test_a_malformed_auxiliary_entry_reports_the_key(tmp_path, monkeypatch) -> None:
    """A bare string is the natural mistake, since optional_symbols one line
    above is a list of plain strings. It used to escape as a TypeError naming
    neither the key nor the file."""
    import sys
    import textwrap

    from librae.orchestration.cli import build_run

    (tmp_path / "config.yaml").write_text(
        textwrap.dedent(
            """\
            strategy:
              symbol: BTCUSDT
              timeframe: 1h
              auxiliary_subscriptions:
                - BTCUSDT
            """
        )
    )
    monkeypatch.setattr(sys, "argv", ["test"])

    with pytest.raises(ValueError, match="auxiliary_subscriptions"):
        build_run("test_strat", str(tmp_path / "run.py"))
