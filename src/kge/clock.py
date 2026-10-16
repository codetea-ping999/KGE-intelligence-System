"""Injectable clocks so that runs can be made fully reproducible."""

from __future__ import annotations

import datetime as dt
import os
from typing import Protocol


class Clock(Protocol):
    def now(self) -> dt.datetime: ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC).replace(microsecond=0)


class FixedClock:
    """Starts at a fixed instant and advances by `step` on every call."""

    def __init__(self, start: dt.datetime, step: dt.timedelta = dt.timedelta(seconds=1)) -> None:
        self._t = start
        self._step = step

    def now(self) -> dt.datetime:
        t = self._t
        self._t = t + self._step
        return t

    def advance(self, delta: dt.timedelta) -> None:
        self._t += delta


DEFAULT_FIXED_START = dt.datetime(2026, 10, 15, tzinfo=dt.UTC)


def clock_from_env() -> Clock:
    """KGE_CLOCK=fixed[:ISO8601] selects a deterministic clock."""
    spec = os.environ.get("KGE_CLOCK", "system")
    if spec.startswith("fixed"):
        _, _, iso = spec.partition(":")
        start = dt.datetime.fromisoformat(iso) if iso else DEFAULT_FIXED_START
        return FixedClock(start)
    return SystemClock()
