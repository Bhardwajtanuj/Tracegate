from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from tracegate.models import RunResult
from tracegate.semconv import TOOL_STATUS

SYSTEM_PROMPT = (
    "You review the execution trace of a tool-using software agent. "
    "Decide whether the agent got stuck repeating equivalent tool calls, whether it drifted "
    "away from the user's request, and how efficiently it reached its result. "
    "Reply with a single JSON object with exactly these keys: "
    '"loop_detected" (boolean), "trajectory_efficiency_score" (number between 0 and 1), '
    '"goal_drift" (boolean), "rationale" (short string).'
)

CompletionFn = Callable[[str, str], str]


class JudgeError(Exception):
    pass


class JudgeVerdict(BaseModel):
    loop_detected: bool
    trajectory_efficiency_score: float = Field(ge=0, le=1)
    goal_drift: bool = False
    rationale: str = ""


def trajectory_payload(result: RunResult, field_limit: int = 500) -> dict[str, Any]:
    steps = []
    for index, span in enumerate(result.spans):
        if span.kind not in {"TOOL", "LLM"}:
            continue
        entry: dict[str, Any] = {
            "step": index,
            "kind": span.kind,
            "name": span.name,
            "latency_s": round(span.latency_s, 4),
        }
        if span.kind == "TOOL":
            entry["arguments"] = str(span.attributes.get("tool.parameters", ""))[:field_limit]
            entry["result_status"] = span.attributes.get(TOOL_STATUS)
        steps.append(entry)
    return {
        "task": result.input,
        "run_status": result.status,
        "steps": steps,
        "final_answer": (result.output or "")[:field_limit],
    }


def _extract_json(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        raise JudgeError("judge response did not contain a JSON object")
    return raw[start : end + 1]


class TrajectoryJudge:
    def __init__(self, complete: CompletionFn):
        self._complete = complete

    def evaluate(self, result: RunResult) -> JudgeVerdict:
        payload = json.dumps(trajectory_payload(result), ensure_ascii=False)
        raw = self._complete(SYSTEM_PROMPT, payload)
        try:
            return JudgeVerdict.model_validate_json(_extract_json(raw))
        except ValidationError as exc:
            raise JudgeError(f"invalid judge verdict: {exc}") from exc
