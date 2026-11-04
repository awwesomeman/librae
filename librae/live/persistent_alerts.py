"""Alerts for a problem seen again cycle after cycle: when it starts, while it lasts, when it ends.

Shaped like Alertmanager's ``repeat_interval`` and ``send_resolved``
(https://prometheus.io/docs/alerting/latest/configuration/): notify when a
problem first fires, repeat it every ``REPEAT_INTERVAL`` while it keeps
firing, and notify once when it resolves. A problem seen clear resolves only
after staying clear for ``REPEAT_INTERVAL``, as Prometheus's
``keep_firing_for`` holds an alert
(https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/),
so one that comes and goes is one problem rather than an alert per change.

The caller names each problem by a key, as PagerDuty's ``dedup_key`` does.
Unlike PagerDuty, a firing whose content differs from the open one's is a new
problem and notifies at once, so a state that goes A, B, then A again alerts
three times. Callers decide what fires and what clears; nothing here reads the
engine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Container, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Shorter than Alertmanager's 4h default: each of these problems needs a
# person, and a stuck order blocks the strategy. Hourly still holds a
# day-long fault on a one-minute feed to 24 alerts instead of 1,440. Also the
# hold before a resolution, so a problem that flaps alerts no faster.
REPEAT_INTERVAL = timedelta(hours=1)

_RESOLVED_TITLE = "Resolved"


@dataclass
class _Open:
    title: str
    message: str
    content: str
    since: datetime
    notified_at: datetime
    seen: int = 1
    clear_since: datetime | None = None


class PersistentAlerts:
    """Open problems by key, alerting through ``send(title, message)`` at ``clock()`` time."""

    def __init__(self, send: Callable[[str, str], None], clock: Callable[[], datetime]) -> None:
        self._send = send
        self._clock = clock
        self._open: dict[Hashable, _Open] = {}

    def fire(self, key: Hashable, title: str, message: str, *, content: str | None = None) -> None:
        """Record that problem ``key`` holds now, alerting if it is new, changed or due again.

        ``content`` is what makes a later firing the same problem, ``message``
        by default. A due repeat carries the latest message, how long the
        problem has lasted and how many times it was seen.
        """
        now = self._clock()
        content = message if content is None else content
        problem = self._open.get(key)
        if problem is None or problem.content != content:
            self._open[key] = _Open(title, message, content, since=now, notified_at=now)
            self._notify(title, message)
            return
        problem.seen += 1
        problem.message = message
        problem.clear_since = None
        if now - problem.notified_at < REPEAT_INTERVAL:
            logger.debug("%s, seen %d times: %s", title, problem.seen, message)
            return
        problem.notified_at = now
        self._notify(
            title,
            f"{message} — still so after {_duration(now - problem.since)}, "
            f"seen {_times(problem.seen)}",
        )

    def hold(self, key: Hashable) -> None:
        """Record that open problem ``key`` holds now without alerting, restarting its hold-down."""
        problem = self._open.get(key)
        if problem is not None:
            problem.clear_since = None

    def clear(self, key: Hashable) -> None:
        """Record that problem ``key`` did not hold now; it resolves once clear for ``REPEAT_INTERVAL``."""
        problem = self._open.get(key)
        if problem is None:
            return
        now = self._clock()
        if problem.clear_since is None:
            problem.clear_since = now
        elif now - problem.clear_since >= REPEAT_INTERVAL:
            self.resolve(key)

    def resolve(self, key: Hashable) -> None:
        """Record that problem ``key`` has ended, alerting once if it was open."""
        problem = self._open.pop(key, None)
        if problem is None:
            return
        ended = problem.clear_since or self._clock()
        message = (
            f"{problem.title} cleared after {_duration(ended - problem.since)}, "
            f"seen {_times(problem.seen)}; last: {problem.message}"
        )
        logger.info("%s: %s", _RESOLVED_TITLE, message)
        self._deliver(_RESOLVED_TITLE, message)

    def retain(self, keys: Container[Hashable]) -> None:
        """End every open problem whose key is not in ``keys``.

        One last seen clear resolves. One still firing is dropped without an
        alert: its last alert said how it ended.
        """
        for key in [key for key in self._open if key not in keys]:
            if self._open[key].clear_since is None:
                del self._open[key]
            else:
                self.resolve(key)

    def _notify(self, title: str, message: str) -> None:
        logger.error("%s: %s", title, message)
        self._deliver(title, message)

    def _deliver(self, title: str, message: str) -> None:
        # Callers sit in order handling: a failed notification must not
        # skip the halt or bookkeeping that follows it.
        try:
            self._send(title, message)
        except Exception:
            logger.exception("Alert %r not sent", title)


def _duration(elapsed: timedelta) -> str:
    minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _times(count: int) -> str:
    return "once" if count == 1 else f"{count} times"
