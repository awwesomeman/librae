"""Calendar-aware observation readiness and staleness.

A completed bar's own timestamp is always about one interval behind wall
clock, and the next one is not due until the venue is open again. Measuring
staleness as a fixed wall-clock age from bar start therefore flags a Friday
daily bar all weekend. These tests pin the expected-boundary semantics that
replace it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from librae.core.readiness import evaluate_observation, next_expected_close


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class TestNextExpectedClose:
    """The boundary is the next observation's completion, not a fixed age."""

    @pytest.mark.parametrize(
        ("label", "last_ts", "timeframe", "calendar_id", "expected"),
        [
            # A Friday daily bar is not late until Monday's session closes.
            ("weekend", "2025-01-03T14:30Z", "D1", "XNYS", "2025-01-06T21:00Z"),
            ("weekday", "2025-01-02T14:30Z", "D1", "XNYS", "2025-01-03T21:00Z"),
            # 2025-07-04 is a holiday, so Thursday's bar waits for Monday.
            # 20:00Z, not 21:00Z, because July is EDT.
            ("holiday_and_dst", "2025-07-03T14:30Z", "D1", "XNYS", "2025-07-07T20:00Z"),
            ("intraday_same_session", "2025-01-03T16:30Z", "H1", "XNYS", "2025-01-03T18:30Z"),
            ("intraday_session_end", "2025-01-03T20:00Z", "H1", "XNYS", "2025-01-06T15:30Z"),
            ("always_open_midnight", "2025-01-03T23:00Z", "H1", "24/7", "2025-01-04T01:00Z"),
            # EST close 21:00Z becomes EDT close 20:00Z across the transition.
            ("dst_spring_forward", "2025-03-07T14:30Z", "D1", "XNYS", "2025-03-10T20:00Z"),
            # session_mode defaults to "extended", so a post-market bar is the
            # common case, not an edge case: it must anchor on the next regular
            # session rather than losing calendar awareness.
            ("post_market", "2025-01-03T22:00Z", "H1", "XNYS", "2025-01-06T15:30Z"),
            # A period spanning many sessions must not resolve to its own close.
            ("multi_session_period", "2025-01-06T14:30Z", "W1", "XNYS", "2025-01-17T21:00Z"),
            # TAIFEX treats a session close as inclusive, so the boundary
            # cannot be detected by asking whether the close has a period.
            ("inclusive_close_daily", "2025-01-02T05:00Z", "D1", "XTAIFEX", "2025-01-03T05:45Z"),
            ("inclusive_close_hourly", "2025-01-02T05:00Z", "H1", "XTAIFEX", "2025-01-02T08:00Z"),
            ("inclusive_close_mid", "2025-01-02T02:00Z", "H1", "XTAIFEX", "2025-01-02T03:45Z"),
        ],
    )
    def test_boundary_follows_the_calendar(
        self,
        label: str,
        last_ts: str,
        timeframe: str,
        calendar_id: str,
        expected: str,
    ) -> None:
        assert next_expected_close(
            _ts(last_ts), timeframe=timeframe, calendar_id=calendar_id
        ) == _ts(expected)

    @pytest.mark.parametrize("calendar_id", ["XNYS", "XTAIFEX", "24/7"])
    @pytest.mark.parametrize("timeframe", ["H1", "D1", "W1"])
    def test_the_deadline_always_moves_forward(self, calendar_id: str, timeframe: str) -> None:
        """The one invariant every calendar shares. A boundary that resolved to
        the observation's own close made a healthy feed look permanently
        stale — how TAIFEX broke before the strict-progress rule."""
        last_ts = _ts("2025-01-02T02:00Z")

        assert next_expected_close(last_ts, timeframe=timeframe, calendar_id=calendar_id) > last_ts

    @pytest.mark.parametrize(
        ("timeframe", "midnight", "expected"),
        [
            ("D1", "2025-01-02T00:00Z", "2025-01-02T21:00Z"),
            ("W1", "2025-01-06T00:00Z", "2025-01-10T21:00Z"),
        ],
    )
    def test_a_midnight_stamp_resolves_one_period_early(
        self, timeframe: str, midnight: str, expected: str
    ) -> None:
        """Pins the timestamp convention the evaluator assumes. Periods are
        stamped at their session open, which is what librae's adapters
        produce; a midnight stamp precedes its own open and so resolves to
        that session's close. Grace absorbs it at these sizes. Documented
        rather than corrected, because guessing which session an off-convention
        stamp belongs to would weaken detection for the conforming case."""
        assert next_expected_close(_ts(midnight), timeframe=timeframe, calendar_id="XNYS") == _ts(
            expected
        )

    def test_each_subscription_uses_its_own_timeframe(self) -> None:
        """The same symbol on H1 and D1 must not share one boundary."""
        hourly = next_expected_close(_ts("2025-01-03T16:30Z"), timeframe="H1", calendar_id="XNYS")
        daily = next_expected_close(_ts("2025-01-03T14:30Z"), timeframe="D1", calendar_id="XNYS")

        assert hourly != daily


class TestEvaluateObservation:
    GRACE = timedelta(hours=2)

    def _evaluate(self, last_ts: str, as_of: str, *, timeframe="D1", calendar_id="XNYS"):
        return evaluate_observation(
            _ts(last_ts),
            as_of=_ts(as_of),
            timeframe=timeframe,
            calendar_id=calendar_id,
            grace=self.GRACE,
        )

    def test_a_friday_bar_is_fresh_all_weekend(self) -> None:
        """The regression this replaces: a fixed wall-clock age alerted here."""
        assert self._evaluate("2025-01-03T14:30Z", "2025-01-05T12:00Z").fresh

    def test_a_friday_bar_is_fresh_until_monday_close_plus_grace(self) -> None:
        assert self._evaluate("2025-01-03T14:30Z", "2025-01-06T22:00Z").fresh

    def test_a_friday_bar_is_stale_once_monday_close_and_grace_pass(self) -> None:
        assert not self._evaluate("2025-01-03T14:30Z", "2025-01-06T23:30Z").fresh

    def test_a_holiday_does_not_make_a_bar_stale(self) -> None:
        assert self._evaluate("2025-07-03T14:30Z", "2025-07-06T12:00Z").fresh

    def test_due_at_is_the_expected_close_plus_grace(self) -> None:
        status = self._evaluate("2025-01-02T14:30Z", "2025-01-03T12:00Z")

        assert status.expected_close == _ts("2025-01-03T21:00Z")
        assert status.due_at == _ts("2025-01-03T23:00Z")

    def test_an_intraday_gap_inside_a_session_still_goes_stale(self) -> None:
        """Calendar awareness must not blunt the detection it exists to keep."""
        status = self._evaluate(
            "2025-01-03T15:30Z", "2025-01-03T20:00Z", timeframe="H1", calendar_id="XNYS"
        )

        assert not status.fresh

    def test_never_observed_subscription_is_not_fresh(self) -> None:
        status = evaluate_observation(
            None,
            as_of=_ts("2025-01-03T15:30Z"),
            timeframe="H1",
            calendar_id="XNYS",
            grace=self.GRACE,
        )

        assert not status.fresh
        assert status.expected_close is None


class TestObservationsOutsideARegularSession:
    """session_mode defaults to "extended", so observations outside the regular
    session are ordinary. They must stay calendar-anchored: falling back to a
    bare interval there would leave the weekend regression this module exists
    to fix in place for the default configuration."""

    def _evaluate(self, as_of: str):
        return evaluate_observation(
            _ts("2025-01-01T05:00Z"),  # outside every XTAIFEX session
            as_of=_ts(as_of),
            timeframe="H1",
            calendar_id="XTAIFEX",
            grace=timedelta(hours=2),
        )

    def test_an_out_of_session_observation_stays_calendar_anchored(self) -> None:
        assert self._evaluate("2025-01-01T06:00Z").calendar_anchored

    def test_it_waits_for_the_next_session_rather_than_one_interval(self) -> None:
        status = self._evaluate("2025-01-01T06:00Z")

        assert status.expected_close > _ts("2025-01-01T07:00Z")
        assert status.fresh

    def test_it_still_detects_a_dead_feed(self) -> None:
        assert not self._evaluate("2025-01-05T05:00Z").fresh

    def test_a_calendar_that_cannot_answer_degrades_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Last-resort guard: staleness monitoring must not become a new way
        for a poll cycle to die if the calendar cannot answer at all."""
        import librae.core.readiness as readiness

        def unusable(*_args, **_kwargs):
            raise ValueError("calendar unavailable")

        monkeypatch.setattr(readiness, "period_close", unusable)
        monkeypatch.setattr(readiness, "next_session_open_after", unusable)

        status = evaluate_observation(
            _ts("2025-01-01T05:00Z"),
            as_of=_ts("2025-01-01T06:00Z"),
            timeframe="H1",
            calendar_id="XTAIFEX",
            grace=timedelta(hours=2),
        )

        assert status.fresh
        assert not status.calendar_anchored


class TestOptionalSubscriptionDeclaration:
    """Required is the default, so an existing run keeps failing closed on a
    missing input. Optional is an explicit, result-affecting declaration."""

    @staticmethod
    def _config(**overrides):
        from librae.core.run_config import AccountConfig, RunConfig

        base = {
            "strategy_name": "t",
            "mode": "backtest",
            "symbols": ["AAA", "BBB"],
            "timeframe": "H1",
            "market": "us_equity",
            "data_source": "test",
            "account": AccountConfig(currency="USD", initial_cash=100_000.0),
        }
        return RunConfig(**{**base, **overrides})

    def test_every_symbol_is_required_by_default(self) -> None:
        assert self._config().optional_symbols == ()

    def test_optional_symbols_must_be_configured_symbols(self) -> None:
        with pytest.raises(ValueError, match="optional_symbols"):
            self._config(optional_symbols=["CCC"])

    def test_duplicates_are_rejected(self) -> None:
        """Length-based validation let [AAA, BBB, AAA] through and produced a
        run with no required inputs at all, which no gate can ever block."""
        with pytest.raises(ValueError, match="duplicates"):
            self._config(optional_symbols=["AAA", "BBB", "AAA"])

    def test_the_primary_symbol_cannot_be_optional(self) -> None:
        """symbols[0] is the default symbol for bare intents and the cadence
        anchor, so the run cannot proceed without it."""
        with pytest.raises(ValueError, match="primary symbol"):
            self._config(optional_symbols=["AAA"])

    def test_a_run_cannot_be_left_without_a_required_input(self) -> None:
        """Covering every symbol necessarily includes the primary one, so the
        primary rule is what enforces this — no separate check needed."""
        with pytest.raises(ValueError, match="primary symbol"):
            self._config(symbols=["AAA"], optional_symbols=["AAA"])

    def test_declaration_order_does_not_change_the_config_hash(self) -> None:
        """Unlike symbols, optional membership has no observable order."""
        forward = self._config(symbols=["AAA", "BBB", "CCC"], optional_symbols=["BBB", "CCC"])
        reversed_ = self._config(symbols=["AAA", "BBB", "CCC"], optional_symbols=["CCC", "BBB"])

        assert forward.config_hash == reversed_.config_hash

    def test_optional_symbols_change_the_config_hash(self) -> None:
        """A strategy that may run without an input is a different strategy,
        so research and live cannot silently share one identity."""
        assert self._config().config_hash != self._config(optional_symbols=["BBB"]).config_hash

    def test_the_strategy_config_file_reaches_run_config(self, tmp_path, monkeypatch) -> None:
        """A documented YAML key that build_run never reads is silently
        dropped: no error, unchanged config_hash, every symbol still required.
        calendar_id had exactly this bug (#210)."""
        import sys
        import textwrap

        from librae.orchestration.cli import build_run

        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """                strategy:
                  symbols: MU,AAPL
                  timeframe: 1d
                  market: us_equity
                  data_source: ibkr
                  optional_symbols:
                    - AAPL
                  account:
                    currency: USD
                    initial_cash: 100000
                """
            )
        )
        monkeypatch.setattr(sys, "argv", ["test"])

        config, _ = build_run("test_strat", str(tmp_path / "run.py"))

        assert config.optional_symbols == ("AAPL",)


class TestDataReadinessGate:
    """The gate's own decision: hold on a missing required input, step over a
    missing optional one, and report each transition once.

    Unit-scoped on purpose. Reaching this gate end-to-end is hard to arrange
    honestly, because the warmup gate and the rolling OHLCV cache both keep a
    partial cycle from getting here first — see the note on sim/live parity in
    the PR. Its single call site is the poll cycle.
    """

    @staticmethod
    def _runner(optional_symbols: tuple[str, ...]):
        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        runner = TestLiveTrader()._make_runner(
            config=_test_cfg(
                mode="sim",
                symbols=["BTCUSDT", "ETHUSDT"],
                warmup_periods=1,
                optional_symbols=optional_symbols,
            ),
        )
        runner._notify = lambda method, **kwargs: runner_alerts.append((method, kwargs))
        return runner

    def test_a_missing_required_input_holds_evaluation(self) -> None:
        global runner_alerts
        runner_alerts = []
        runner = self._runner(optional_symbols=())

        assert runner._report_data_readiness(["ETHUSDT"]) is True

    def test_a_missing_optional_input_does_not_hold_evaluation(self) -> None:
        global runner_alerts
        runner_alerts = []
        runner = self._runner(optional_symbols=("ETHUSDT",))

        assert runner._report_data_readiness(["ETHUSDT"]) is False
        assert runner_alerts == []

    def test_the_alert_is_edge_triggered_and_re_arms(self) -> None:
        global runner_alerts
        runner_alerts = []
        runner = self._runner(optional_symbols=())

        runner._report_data_readiness(["ETHUSDT"])
        runner._report_data_readiness(["ETHUSDT"])
        runner._report_data_readiness([])
        runner._report_data_readiness(["ETHUSDT"])

        titles = [kw["title"] for method, kw in runner_alerts if method == "send_alert"]
        assert len(titles) == 2, "one alert per episode, not one per cycle"


class TestOptionalSymbolsReleaseTheWarmupGate:
    """warmup_ready held the run until every symbol had history. An optional
    subscription must not be able to hold it."""

    @staticmethod
    def _run(optional_symbols: tuple[str, ...]):
        import pandas as pd
        from librae.core.strategy import Strategy

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        def frame(n: int) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "ts": pd.date_range("2025-01-01T00:00Z", periods=n, freq="h", tz="UTC"),
                    "open": [100.0] * n,
                    "high": [101.0] * n,
                    "low": [99.0] * n,
                    "close": [100.0] * n,
                    "volume": [1_000.0] * n,
                }
            )

        seen: list[tuple[str, ...]] = []

        class RecordEvaluations(Strategy):
            def on_bar(self, ctx):
                seen.append(ctx.available_symbols)
                return []

        runner = TestLiveTrader()._make_runner(
            strategy=RecordEvaluations(),
            fetcher={
                "BTCUSDT": lambda *a, **k: frame(6),
                "ETHUSDT": lambda *a, **k: frame(0),
            },
            config=_test_cfg(
                mode="sim",
                symbols=["BTCUSDT", "ETHUSDT"],
                warmup_periods=1,
                optional_symbols=optional_symbols,
            ),
        )
        runner._poll_cycle()
        return seen

    def test_a_required_symbol_without_history_still_holds_warmup(self) -> None:
        assert self._run(optional_symbols=()) == []

    def test_an_optional_symbol_without_history_releases_warmup(self) -> None:
        seen = self._run(optional_symbols=("ETHUSDT",))

        assert seen, "the strategy should run on the inputs that did arrive"
        assert all("ETHUSDT" not in available for available in seen)


class TestSimulationHoldsOnStaleRequiredData:
    """Simulation runs against a live feed, so wall-clock staleness means the
    same thing it does in live. It used to skip the stale check and evaluate
    on the last known bars; live and sim now fail closed identically.

    (Backtest is unaffected: it replays history, where staleness relative to
    wall clock has no meaning, and never reaches the poll cycle.)
    """

    CLOCK = datetime(2025, 1, 2, 0, 0, tzinfo=UTC)

    @classmethod
    def _run(cls, optional_symbols: tuple[str, ...]):
        import pandas as pd
        from librae.core.strategy import Strategy

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        def frame(end: datetime, n: int = 6) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "ts": pd.date_range(end=end, periods=n, freq="h", tz="UTC"),
                    "open": [100.0] * n,
                    "high": [101.0] * n,
                    "low": [99.0] * n,
                    "close": [100.0] * n,
                    "volume": [1_000.0] * n,
                }
            )

        seen: list[tuple[str, ...]] = []

        class RecordEvaluations(Strategy):
            def on_bar(self, ctx):
                seen.append(ctx.available_symbols)
                return []

        runner = TestLiveTrader()._make_runner(
            strategy=RecordEvaluations(),
            fetcher={
                # One interval behind the clock: fresh.
                "BTCUSDT": lambda *a, **k: frame(cls.CLOCK - timedelta(hours=1)),
                # Ten intervals behind: past its expected close plus grace.
                "ETHUSDT": lambda *a, **k: frame(cls.CLOCK - timedelta(hours=10)),
            },
            config=_test_cfg(
                mode="sim",
                symbols=["BTCUSDT", "ETHUSDT"],
                warmup_periods=1,
                optional_symbols=optional_symbols,
            ),
            clock=lambda: cls.CLOCK,
        )
        # The shared test runner relaxes this to 100 so fixtures far from wall
        # clock never trip staleness; this suite is about staleness itself.
        runner.STALE_DATA_TOLERANCE_BARS = 2
        runner._poll_cycle()
        return seen

    def test_a_stale_required_input_holds_evaluation(self) -> None:
        assert self._run(optional_symbols=()) == []

    def test_a_stale_optional_input_is_stepped_over(self) -> None:
        seen = self._run(optional_symbols=("ETHUSDT",))

        assert seen, "the fresh input should still be evaluated"
        assert all("ETHUSDT" not in available for available in seen)


class TestOptionalSymbolAcrossWarmupCycles:
    """The integration the unit tests cannot see: the readiness gate, the
    warmup gate and the rolling cache all act on the same cycle. An optional
    symbol that starts empty, then arrives short, then completes, must never
    raise and must never reach the strategy under-warmed."""

    @staticmethod
    def _bars(n: int):
        import pandas as pd

        return pd.DataFrame(
            {
                "ts": pd.date_range("2025-01-01T00:00Z", periods=n, freq="h", tz="UTC"),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [1_000.0] * n,
            }
        )

    def test_optional_symbol_joins_only_once_it_is_warm(self) -> None:
        from librae.core.strategy import Strategy

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        # BTC starts short of warmup so the first cycle reports incomplete
        # warmup; the "warmup ready" summary on the next cycle is what used to
        # raise KeyError for a symbol the cache never got a key for.
        state = {"btc": 1, "eth": 0}
        seen: list[tuple[str, ...]] = []

        class RecordEvaluations(Strategy):
            def on_bar(self, ctx):
                seen.append(ctx.available_symbols)
                return []

        runner = TestLiveTrader()._make_runner(
            strategy=RecordEvaluations(),
            fetcher={
                "BTCUSDT": lambda *a, **k: self._bars(state["btc"]),
                "ETHUSDT": lambda *a, **k: self._bars(state["eth"]),
            },
            config=_test_cfg(
                mode="sim",
                symbols=["BTCUSDT", "ETHUSDT"],
                warmup_periods=3,
                optional_symbols=("ETHUSDT",),
            ),
        )

        # Cycle 1: the required feed is short of warmup, so incomplete warmup
        # is reported and the next cycle will print the "warmup ready" summary.
        runner._poll_cycle()
        # Cycle 2: the required feed completes while the optional one still has
        # nothing. This is the cycle that used to raise KeyError, because that
        # summary indexed the cache for every symbol and the optional one never
        # got a key.
        state["btc"] = 6
        runner._poll_cycle()
        # Cycle 3: the optional feed arrives, but short of the requirement.
        state["btc"], state["eth"] = 7, 2
        runner._poll_cycle()
        under_warmed = list(seen)
        # Cycle 4: it completes.
        state["btc"], state["eth"] = 8, 8
        runner._poll_cycle()

        assert under_warmed, "the required symbol must keep evaluating throughout"
        assert all("ETHUSDT" not in available for available in under_warmed), (
            "an under-warmed optional symbol must not reach the strategy"
        )
        assert any("ETHUSDT" in available for available in seen[len(under_warmed) :]), (
            "it must join once its warmup gap closes"
        )
