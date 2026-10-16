from __future__ import annotations

import json
from typing import Any


class LoopDetected(Exception):
    def __init__(self, tool: str, period: int, repeats: int):
        super().__init__(
            f"repeating pattern detected at tool {tool!r}: cycle of {period} call(s) "
            f"repeated {repeats} times"
        )
        self.tool = tool
        self.period = period
        self.repeats = repeats


class StepLimitExceeded(Exception):
    def __init__(self, limit: int):
        super().__init__(f"tool call limit of {limit} reached")
        self.limit = limit


class LoopGuard:
    def __init__(self, max_steps: int, repeats: int = 3, max_period: int = 3):
        if max_steps < 1 or repeats < 2 or max_period < 1:
            raise ValueError("max_steps >= 1, repeats >= 2 and max_period >= 1 are required")
        self.max_steps = max_steps
        self.repeats = repeats
        self.max_period = max_period
        self._history: list[tuple[str, str]] = []

    @property
    def steps(self) -> int:
        return len(self._history)

    def observe(self, tool: str, arguments: Any) -> None:
        if len(self._history) >= self.max_steps:
            raise StepLimitExceeded(self.max_steps)
        fingerprint = json.dumps(arguments, sort_keys=True, default=str)
        self._history.append((tool, fingerprint))
        period = self._cycle_period()
        if period:
            raise LoopDetected(tool, period, self.repeats)

    def _cycle_period(self) -> int | None:
        size = len(self._history)
        for period in range(1, self.max_period + 1):
            window_size = period * self.repeats
            if size < window_size:
                continue
            window = self._history[-window_size:]
            unit = window[:period]
            if all(window[index] == unit[index % period] for index in range(window_size)):
                return period
        return None
