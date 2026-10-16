from __future__ import annotations

from pydantic import BaseModel

from tracegate.evaluation.dataset import GoldenCase
from tracegate.evaluation.judge import JudgeVerdict
from tracegate.models import RunResult

ALIGNED_STATUSES = {"ok", "tool_error"}


class CaseMetrics(BaseModel):
    completed: bool
    tool_selection_correct: bool
    schema_checked: int
    schema_aligned: int
    tool_call_count: int
    within_call_limit: bool
    loop_free: bool
    unhandled_error: bool
    within_budget: bool
    answer_match: bool
    trajectory_efficiency: float
    latency_s: float
    cost_usd: float

    @property
    def schema_alignment(self) -> float:
        if self.schema_checked == 0:
            return 1.0
        return self.schema_aligned / self.schema_checked

    @property
    def passed(self) -> bool:
        return (
            self.completed
            and self.tool_selection_correct
            and self.within_call_limit
            and self.answer_match
        )

    def failures(self) -> list[str]:
        checks = {
            "completed": self.completed,
            "tool_selection_correct": self.tool_selection_correct,
            "within_call_limit": self.within_call_limit,
            "loop_free": self.loop_free,
            "within_budget": self.within_budget,
            "answer_match": self.answer_match,
        }
        failed = [name for name, value in checks.items() if not value]
        if self.unhandled_error:
            failed.append("unhandled_error")
        return failed


class CaseResult(BaseModel):
    case: GoldenCase
    run: RunResult
    metrics: CaseMetrics
    judge: JudgeVerdict | None = None
    judge_error: str | None = None


def compute_metrics(case: GoldenCase, run: RunResult) -> CaseMetrics:
    executed = [call for call in run.tool_calls if call.status != "blocked"]
    invoked = {call.name for call in executed}
    output = (run.output or "").lower()
    expected_count = len(case.expected_tools)
    actual_count = len(run.tool_calls)
    if actual_count == 0:
        efficiency = 1.0 if expected_count == 0 else 0.0
    else:
        efficiency = min(1.0, expected_count / actual_count)
    return CaseMetrics(
        completed=run.status == "success",
        tool_selection_correct=invoked == set(case.expected_tools),
        schema_checked=len(executed),
        schema_aligned=sum(1 for call in executed if call.status in ALIGNED_STATUSES),
        tool_call_count=actual_count,
        within_call_limit=actual_count <= case.max_tool_calls,
        loop_free=run.status not in {"loop_detected", "step_limit"},
        unhandled_error=run.status == "error",
        within_budget=run.status != "budget_exceeded",
        answer_match=all(text.lower() in output for text in case.expected_substrings),
        trajectory_efficiency=efficiency,
        latency_s=run.latency_s,
        cost_usd=run.cost_usd,
    )
