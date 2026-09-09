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


class TestObservationsTheCalendarCannotPlace:
    """Staleness monitoring is a safety check; it must not become a new way
    for a poll cycle to die. An extended-session feed, or simply odd data,
    can carry a timestamp the calendar has no session for."""

    def _evaluate(self, as_of: str):
        return evaluate_observation(
            _ts("2025-01-01T05:00Z"),  # outside every XTAIFEX session
            as_of=_ts(as_of),
            timeframe="H1",
            calendar_id="XTAIFEX",
            grace=timedelta(hours=2),
        )

    def test_it_falls_back_instead_of_raising(self) -> None:
        status = self._evaluate("2025-01-01T06:00Z")

        assert status.fresh
        assert not status.calendar_anchored

    def test_the_fallback_still_detects_a_dead_feed(self) -> None:
        status = self._evaluate("2025-01-02T05:00Z")

        assert not status.fresh
        assert not status.calendar_anchored

    def test_a_placeable_observation_stays_calendar_anchored(self) -> None:
        status = evaluate_observation(
            _ts("2025-01-03T14:30Z"),
            as_of=_ts("2025-01-05T12:00Z"),
            timeframe="D1",
            calendar_id="XNYS",
            grace=timedelta(hours=2),
        )

        assert status.calendar_anchored


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

    def test_optional_symbols_change_the_config_hash(self) -> None:
        """A strategy that may run without an input is a different strategy,
        so research and live cannot silently share one identity."""
        assert self._config().config_hash != self._config(optional_symbols=["BBB"]).config_hash


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
