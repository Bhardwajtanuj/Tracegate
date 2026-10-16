from __future__ import annotations

import json

import pytest

from tests import policies
from tracegate import AgentHarness, Settings
from tracegate.evaluation import (
    EvaluationRunner,
    GoldenCase,
    Threshold,
    build_scorecard,
    compute_metrics,
    load_dataset,
    load_thresholds,
)
from tracegate.evaluation.scorecard import percentile
from tracegate.reference import ReferenceAgent


def case(**overrides) -> GoldenCase:
    base = {
        "id": "c1",
        "input": "Fetch revenue numbers for Q3",
        "expected_tools": ["query_database"],
        "max_tool_calls": 3,
        "expected_substrings": ["412000"],
    }
    base.update(overrides)
    return GoldenCase(**base)


def evaluate(harness, golden: GoldenCase, policy=policies.keyword_policy, **runner_kwargs):
    runner = EvaluationRunner(
        harness, lambda: ReferenceAgent(policy=policy), concurrency=1, **runner_kwargs
    )
    return runner.run([golden])


def test_golden_dataset_passes_every_release_gate(harness):
    cases = load_dataset("datasets/golden.jsonl")
    report = EvaluationRunner(harness, ReferenceAgent, concurrency=4).run(cases)
    assert report.scorecard.verdict == "pass"
    assert report.scorecard.passed_cases == len(cases)
    assert report.scorecard.metrics["tool_selection_accuracy"] == 1.0
    assert [result.case.id for result in report.results] == [c.id for c in cases]


def test_perfect_case_metrics(harness):
    metrics = evaluate(harness, case()).results[0].metrics
    assert metrics.passed
    assert metrics.schema_alignment == 1.0
    assert metrics.trajectory_efficiency == 1.0
    assert metrics.failures() == []


def test_wrong_tool_fails_selection(harness):
    report = evaluate(harness, case(expected_tools=["send_email"]))
    assert not report.results[0].metrics.tool_selection_correct
    assert report.scorecard.verdict == "fail"
    assert "tool_selection_correct" in report.results[0].metrics.failures()


def test_loop_is_reported_as_blocking(harness):
    report = evaluate(harness, case(max_tool_calls=10), policies.looping_policy)
    metrics = report.results[0].metrics
    assert not metrics.loop_free
    assert not metrics.completed
    assert report.scorecard.metrics["loop_rate"] == 1.0
    assert report.scorecard.verdict == "fail"
    assert "loop_free" in metrics.failures()


def test_unhandled_error_and_budget_flags():
    crashing = AgentHarness(Settings(_env_file=None))
    try:
        report = evaluate(crashing, case(), policies.crashing_policy)
    finally:
        crashing.close()
    assert report.results[0].metrics.unhandled_error
    assert report.scorecard.metrics["unhandled_error_rate"] == 1.0
    tight = AgentHarness(Settings(_env_file=None, budget_usd=0.000001))
    try:
        report = evaluate(tight, case())
    finally:
        tight.close()
    assert not report.results[0].metrics.within_budget
    assert report.scorecard.metrics["budget_breach_rate"] == 1.0


def test_schema_alignment_counts_argument_errors(harness):
    report = evaluate(harness, case(max_tool_calls=5), policies.fumbling_policy)
    metrics = report.results[0].metrics
    assert (metrics.schema_checked, metrics.schema_aligned) == (2, 1)
    assert report.scorecard.metrics["schema_alignment"] == 0.5
    assert report.scorecard.verdict == "fail"


def test_call_limit_and_efficiency(harness):
    report = evaluate(
        harness, case(expected_substrings=[], max_tool_calls=2), policies.wasteful_policy
    )
    metrics = report.results[0].metrics
    assert metrics.tool_call_count == 3
    assert not metrics.within_call_limit
    assert metrics.trajectory_efficiency == pytest.approx(1 / 3)
    assert not metrics.passed


def test_out_of_scope_case_expects_no_tools(harness):
    golden = case(
        input="What is the weather like in Paris",
        expected_tools=[],
        max_tool_calls=0,
        expected_substrings=["only help"],
    )
    metrics = evaluate(harness, golden).results[0].metrics
    assert metrics.passed
    assert metrics.trajectory_efficiency == 1.0


def test_warnings_do_not_block(harness):
    thresholds = [
        Threshold(metric="mean_cost_usd", comparator="max", value=0.0, severity="warn", action="x")
    ]
    report = evaluate(harness, case(), thresholds=thresholds)
    assert report.scorecard.verdict == "pass_with_warnings"


def test_missing_metric_fails_its_check(harness):
    thresholds = [
        Threshold(
            metric="judge_efficiency", comparator="min", value=0.5, severity="block", action="x"
        )
    ]
    report = evaluate(harness, case(), thresholds=thresholds)
    assert report.scorecard.checks[0].actual is None
    assert report.scorecard.verdict == "fail"


def test_report_files(harness, tmp_path):
    report = evaluate(harness, case(expected_tools=["send_email"]))
    report.write(tmp_path / "out")
    scorecard = json.loads((tmp_path / "out" / "scorecard.json").read_text())
    assert scorecard["verdict"] == "fail"
    markdown = (tmp_path / "out" / "scorecard.md").read_text()
    assert "## Failing cases" in markdown
    lines = (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["case"]["id"] == "c1"


def test_percentile_uses_nearest_rank():
    assert percentile([], 0.95) == 0.0
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([float(n) for n in range(1, 21)], 0.95) == 19.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0


def test_dataset_validation(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a", "input": "x"}\n{"id": "a", "input": "y"}\n')
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(path)
    path.write_text('{"input": "x"}\n')
    with pytest.raises(ValueError, match=":1:"):
        load_dataset(path)
    path.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        load_dataset(path)


def test_threshold_file_round_trip(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(
        json.dumps(
            [
                {
                    "metric": "pass_rate",
                    "comparator": "min",
                    "value": 1.0,
                    "severity": "block",
                    "action": "stop",
                }
            ]
        )
    )
    assert load_thresholds(path)[0].metric == "pass_rate"


def test_runner_rejects_bad_input(harness):
    with pytest.raises(ValueError):
        EvaluationRunner(harness, ReferenceAgent, concurrency=0)
    with pytest.raises(ValueError):
        EvaluationRunner(harness, ReferenceAgent).run([])
    with pytest.raises(ValueError):
        build_scorecard([])


def test_compute_metrics_ignores_blocked_calls_for_selection(harness):
    run = harness.run(ReferenceAgent(policy=policies.looping_policy), "anything")
    metrics = compute_metrics(case(expected_substrings=[]), run)
    assert metrics.tool_selection_correct
    assert not metrics.loop_free
