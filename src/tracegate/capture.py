from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tracegate.config import Settings
from tracegate.models import RunResult


class CaptureSink:
    def __init__(self, path: str | Path, settings: Settings):
        self.path = Path(path)
        self.settings = settings
        self._lock = threading.Lock()
        self._seen = {record["input"] for record in read_candidates(self.path)}

    def reasons(self, result: RunResult) -> list[str]:
        found: list[str] = []
        if result.status != "success":
            found.append(f"status:{result.status}")
        if any(call.status == "argument_error" for call in result.tool_calls):
            found.append("argument_errors")
        if result.cost_usd >= self.settings.capture_budget_ratio * self.settings.budget_usd:
            found.append("budget_pressure")
        if len(result.tool_calls) >= self.settings.capture_step_ratio * self.settings.max_steps:
            found.append("high_step_count")
        return found

    def consider(self, result: RunResult) -> list[str]:
        found = self.reasons(result)
        if not found:
            return found
        record: dict[str, Any] = {
            "input": result.input,
            "reasons": found,
            "status": result.status,
            "observed_tools": [call.name for call in result.tool_calls],
            "cost_usd": result.cost_usd,
            "trace_id": result.trace_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "expected_tools": None,
        }
        with self._lock:
            if result.input in self._seen:
                return found
            self._seen.add(result.input)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return found


def read_candidates(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
