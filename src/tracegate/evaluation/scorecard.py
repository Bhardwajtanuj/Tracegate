from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from tracegate.evaluation.metrics import CaseResult

Verdict = Literal["pass", "pass_with_warnings", "fail"]


class Threshold(BaseModel):
    metric: str
    comparator: Literal["min", "max"]
    value: float
    severity: Literal["block", "warn"]
    action: str


class Check(BaseModel):
    metric: str
    comparator: Literal["min", "max"]
    threshold: float
    actual: float | None
    passed: bool
    severity: Literal["block", "warn"]
    action: str


class Scorecard(BaseModel):
    cases: int
    passed_cases: int
    metrics: dict[str, float]
    checks: list[Check]
    verdict: Verdict

    def to_markdown(self) -> str:
        lines = [
            "# Agent evaluation scorecard",
            "",
            f"Verdict: **{self.verdict.replace('_', ' ').upper()}**",
            "",
            f"Cases: {self.cases}, passed: {self.passed_cases}",
            "",
            "| Metric | Target | Actual | Result | Action on failure |",
            "| --- | --- | --- | --- | --- |",
        ]
        for check in self.checks:
            symbol = ">=" if check.comparator == "min" else "<="
            actual = "n/a" if check.actual is None else f"{check.actual:.4f}"
            if check.passed:
                result = "pass"
            else:
                result = "FAIL" if check.severity == "block" else "WARN"
            lines.append(
                f"| {check.metric} | {symbol} {check.threshold:g} | {actual} | {result} "
                f"| {check.action} |"
            )
        return "\n".join(lines) + "\n"


DEFAULT_THRESHOLDS: list[Threshold] = [
    Threshold(
        metric="tool_selection_accuracy",
        comparator="min",
        value=0.92,
        severity="block",
        action="Block deployment: revise tool names, descriptions and parameter schemas",
    ),
    Threshold(
        metric="schema_alignment",
        comparator="min",
        value=0.92,
        severity="block",
        action="Block deployment: tighten argument schemas or prompt guidance",
    ),
    Threshold(
        metric="unhandled_error_rate",
        comparator="max",
        value=0.0,
        severity="block",
        action="Block deployment: wrap the failing code path",
    ),
    Threshold(
        metric="loop_rate",
        comparator="max",
        value=0.0,
        severity="block",
        action="Block deployment: fix the planning logic that repeats calls",
    ),
    Threshold(
        metric="budget_breach_rate",
        comparator="max",
        value=0.0,
        severity="block",
        action="Block deployment: reduce context growth or raise the budget deliberately",
    ),
    Threshold(
        metric="call_limit_compliance",
        comparator="min",
        value=1.0,
        severity="block",
        action="Block deployment: investigate redundant tool calls",
    ),
    Threshold(
        metric="answer_match_rate",
        comparator="min",
        value=0.9,
        severity="block",
        action="Block deployment: review answer synthesis",
    ),
    Threshold(
        metric="latency_p95_s",
        comparator="max",
        value=12.0,
        severity="warn",
        action="Warning: review dependencies and routing",
    ),
    Threshold(
        metric="mean_trajectory_efficiency",
        comparator="min",
        value=0.7,
        severity="warn",
        action="Warning: review planning prompts and tool granularity",
    ),
    Threshold(
        metric="mean_cost_usd",
        comparator="max",
        value=0.25,
        severity="warn",
        action="Warning: review context size and model choice",
    ),
]


def load_thresholds(path: str | Path) -> list[Threshold]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Threshold.model_validate(entry) for entry in data]


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(math.ceil(quantile * len(ordered)), 1)
    return ordered[rank - 1]


def _rate(flags: Iterable[bool]) -> float:
    items = list(flags)
    return sum(1 for flag in items if flag) / len(items)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def aggregate(results: Sequence[CaseResult]) -> dict[str, float]:
    if not results:
        raise ValueError("no results to aggregate")
    items = [result.metrics for result in results]
    checked = sum(item.schema_checked for item in items)
    aligned = sum(item.schema_aligned for item in items)
    metrics = {
        "pass_rate": _rate(item.passed for item in items),
        "tool_selection_accuracy": _rate(item.tool_selection_correct for item in items),
        "schema_alignment": 1.0 if checked == 0 else aligned / checked,
        "unhandled_error_rate": _rate(item.unhandled_error for item in items),
        "loop_rate": _rate(not item.loop_free for item in items),
        "budget_breach_rate": _rate(not item.within_budget for item in items),
        "call_limit_compliance": _rate(item.within_call_limit for item in items),
        "answer_match_rate": _rate(item.answer_match for item in items),
        "mean_trajectory_efficiency": _mean([item.trajectory_efficiency for item in items]),
        "latency_p95_s": percentile([item.latency_s for item in items], 0.95),
        "mean_cost_usd": _mean([item.cost_usd for item in items]),
    }
    verdicts = [result.judge for result in results if result.judge is not None]
    if verdicts:
        metrics["judge_efficiency"] = _mean([v.trajectory_efficiency_score for v in verdicts])
        metrics["judge_loop_rate"] = _rate(v.loop_detected for v in verdicts)
    return metrics


def evaluate_thresholds(metrics: dict[str, float], thresholds: Sequence[Threshold]) -> list[Check]:
    checks: list[Check] = []
    tolerance = 1e-9
    for threshold in thresholds:
        actual = metrics.get(threshold.metric)
        if actual is None:
            passed = False
        elif threshold.comparator == "min":
            passed = actual >= threshold.value - tolerance
        else:
            passed = actual <= threshold.value + tolerance
        checks.append(
            Check(
                metric=threshold.metric,
                comparator=threshold.comparator,
                threshold=threshold.value,
                actual=actual,
                passed=passed,
                severity=threshold.severity,
                action=threshold.action,
            )
        )
    return checks


def build_scorecard(
    results: Sequence[CaseResult], thresholds: Sequence[Threshold] | None = None
) -> Scorecard:
    metrics = aggregate(results)
    checks = evaluate_thresholds(metrics, DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    failed = [check for check in checks if not check.passed]
    if any(check.severity == "block" for check in failed):
        verdict: Verdict = "fail"
    elif failed:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"
    return Scorecard(
        cases=len(results),
        passed_cases=sum(1 for result in results if result.metrics.passed),
        metrics=metrics,
        checks=checks,
        verdict=verdict,
    )
