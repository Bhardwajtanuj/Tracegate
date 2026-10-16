from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests import policies
from tracegate import AgentHarness, CaptureSink, Settings
from tracegate.cli import load_factory, main
from tracegate.evaluation import (
    EvaluationRunner,
    GoldenCase,
    JudgeError,
    TrajectoryJudge,
    load_dataset,
    promote_candidates,
)
from tracegate.evaluation.judge import trajectory_payload
from tracegate.integrations import anthropic_invoke, openai_completion, openai_invoke
from tracegate.openai_agent import OpenAIToolAgent
from tracegate.reference import ReferenceAgent, build_database, build_registry


def sample_run(harness, prompt="Fetch revenue numbers for Q3", policy=policies.keyword_policy):
    return harness.run(ReferenceAgent(policy=policy), prompt)


def test_judge_parses_fenced_json_and_sends_a_compact_trajectory(harness):
    seen = {}

    def complete(system, user):
        seen["system"], seen["user"] = system, user
        return '```json\n{"loop_detected": false, "trajectory_efficiency_score": 0.9}\n```'

    verdict = TrajectoryJudge(complete).evaluate(sample_run(harness))
    assert verdict.trajectory_efficiency_score == 0.9
    assert not verdict.goal_drift
    payload = json.loads(seen["user"])
    assert payload["task"] == "Fetch revenue numbers for Q3"
    assert [step["kind"] for step in payload["steps"]] == ["LLM", "TOOL", "LLM"]
    assert payload["steps"][1]["result_status"] == "ok"
    assert "JSON" in seen["system"]


def test_judge_rejects_malformed_output(harness):
    run = sample_run(harness)
    with pytest.raises(JudgeError):
        TrajectoryJudge(lambda system, user: "no json here").evaluate(run)
    with pytest.raises(JudgeError):
        TrajectoryJudge(
            lambda system, user: '{"loop_detected": false, "trajectory_efficiency_score": 4}'
        ).evaluate(run)


def test_trajectory_payload_clips_long_fields(harness):
    run = sample_run(harness)
    payload = trajectory_payload(run, field_limit=10)
    assert len(payload["steps"][1]["arguments"]) <= 10


def test_runner_aggregates_judge_scores_and_survives_judge_failures(harness):
    golden = GoldenCase(
        id="c",
        input="Fetch revenue numbers for Q3",
        expected_tools=["query_database"],
        expected_substrings=["412000"],
    )
    good = TrajectoryJudge(
        lambda s, u: '{"loop_detected": false, "trajectory_efficiency_score": 0.8}'
    )
    report = EvaluationRunner(harness, ReferenceAgent, judge=good, concurrency=1).run([golden])
    assert report.scorecard.metrics["judge_efficiency"] == pytest.approx(0.8)
    assert report.scorecard.metrics["judge_loop_rate"] == 0.0
    broken = TrajectoryJudge(lambda s, u: "nonsense")
    report = EvaluationRunner(harness, ReferenceAgent, judge=broken, concurrency=1).run([golden])
    assert report.results[0].judge is None
    assert report.results[0].judge_error
    assert "judge_efficiency" not in report.scorecard.metrics


def test_capture_flags_failures_and_deduplicates(settings, tmp_path):
    sink = CaptureSink(tmp_path / "candidates.jsonl", settings)
    harness = AgentHarness(settings, sink=sink)
    try:
        sample_run(harness)
        sample_run(harness, "anything", policies.looping_policy)
        sample_run(harness, "anything", policies.looping_policy)
        sample_run(harness, "fumble", policies.fumbling_policy)
    finally:
        harness.close()
    records = [
        json.loads(line) for line in (tmp_path / "candidates.jsonl").read_text().splitlines()
    ]
    assert len(records) == 2
    assert records[0]["reasons"] == ["status:loop_detected"]
    assert records[0]["observed_tools"] == ["query_database"] * 3
    assert records[0]["expected_tools"] is None
    assert records[1]["reasons"] == ["argument_errors"]
    reopened = CaptureSink(tmp_path / "candidates.jsonl", settings)
    assert reopened._seen == {"anything", "fumble"}


def test_capture_flags_step_pressure(tmp_path):
    settings = Settings(_env_file=None, max_steps=3)
    sink = CaptureSink(tmp_path / "c.jsonl", settings)
    harness = AgentHarness(settings, sink=sink)
    try:
        result = sample_run(harness, "anything", policies.wasteful_policy)
    finally:
        harness.close()
    assert result.status == "success"
    assert sink.reasons(result) == ["high_step_count"]


def test_capture_flags_budget_pressure(tmp_path):
    settings = Settings(_env_file=None, budget_usd=0.000001)
    sink = CaptureSink(tmp_path / "c.jsonl", settings)
    harness = AgentHarness(settings, sink=sink)
    try:
        result = sample_run(harness)
    finally:
        harness.close()
    assert sink.reasons(result) == ["status:budget_exceeded", "budget_pressure"]


def test_promotion_moves_reviewed_candidates(tmp_path):
    candidates = tmp_path / "candidates.jsonl"
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text(
        '{"id": "base", "input": "Send alert email to admin", "expected_tools": ["send_email"]}\n'
    )
    rows = [
        {"input": "new reviewed", "trace_id": "abcdef123456", "expected_tools": ["query_database"]},
        {"input": "still pending", "trace_id": "999", "expected_tools": None},
        {"input": "Send alert email to admin", "trace_id": "dup", "expected_tools": ["send_email"]},
    ]
    candidates.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert promote_candidates(candidates, dataset) == 1
    cases = load_dataset(dataset)
    assert [item.id for item in cases] == ["base", "captured-abcdef12"]
    assert cases[1].max_tool_calls == 3
    remaining = [json.loads(line) for line in candidates.read_text().splitlines()]
    assert [row["input"] for row in remaining] == ["still pending"]


def test_cli_eval_passes_and_writes_reports(tmp_path, capsys):
    code = main(["eval", "--dataset", "datasets/golden.jsonl", "--out", str(tmp_path / "r")])
    assert code == 0
    assert "Verdict: **PASS**" in capsys.readouterr().out
    assert (tmp_path / "r" / "scorecard.json").exists()


def test_cli_eval_fails_with_exit_code_one(tmp_path, capsys):
    dataset = tmp_path / "bad.jsonl"
    dataset.write_text(
        json.dumps(
            {"id": "x", "input": "Send alert email to admin", "expected_tools": ["query_database"]}
        )
        + "\n"
    )
    assert main(["eval", "--dataset", str(dataset), "--out", str(tmp_path / "r")]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_eval_with_thresholds_capture_and_custom_agent(tmp_path):
    thresholds = tmp_path / "t.json"
    thresholds.write_text(
        json.dumps(
            [
                {
                    "metric": "pass_rate",
                    "comparator": "min",
                    "value": 1.0,
                    "severity": "block",
                    "action": "a",
                }
            ]
        )
    )
    code = main(
        [
            "eval",
            "--dataset",
            "datasets/golden.jsonl",
            "--out",
            str(tmp_path / "r"),
            "--thresholds",
            str(thresholds),
            "--capture",
            str(tmp_path / "cand.jsonl"),
            "--agent",
            "tracegate.reference:create_agent",
            "--concurrency",
            "2",
        ]
    )
    assert code == 0
    assert not (tmp_path / "cand.jsonl").exists()


def test_cli_run_prints_result(capsys):
    assert main(["run", "Send alert email to admin"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert "spans" not in payload
    assert main(["run", "Send alert email to admin", "--spans"]) == 0
    assert json.loads(capsys.readouterr().out)["spans"]


def test_cli_promote(tmp_path, capsys):
    candidates = tmp_path / "c.jsonl"
    dataset = tmp_path / "d.jsonl"
    candidates.write_text(json.dumps({"input": "q", "trace_id": "t1", "expected_tools": []}) + "\n")
    assert main(["promote", "--candidates", str(candidates), "--dataset", str(dataset)]) == 0
    assert "promoted 1" in capsys.readouterr().out


def test_cli_reports_configuration_errors(tmp_path, capsys):
    assert main(["eval", "--dataset", str(tmp_path / "missing.jsonl")]) == 2
    assert main(["run", "x", "--agent", "badspec"]) == 2
    assert main(["run", "x", "--agent", "tracegate.reference:missing"]) == 2
    assert "error:" in capsys.readouterr().err


def test_load_factory_default():
    assert load_factory(None)().tools.names() == [
        "query_database",
        "send_email",
        "search_documents",
    ]


def make_openai_response(content=None, tool_calls=None, prompt=10, completion=5):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def make_tool_call(identifier, name, arguments):
    return SimpleNamespace(id=identifier, function=SimpleNamespace(name=name, arguments=arguments))


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def test_openai_tool_agent_round_trip(harness):
    registry = build_registry(build_database(), [])
    client = FakeOpenAI(
        [
            make_openai_response(
                tool_calls=[
                    make_tool_call(
                        "call_1",
                        "query_database",
                        json.dumps({"query_string": "SELECT SUM(amount) AS total FROM revenue"}),
                    ),
                    make_tool_call("call_2", "query_database", "{not json"),
                ]
            ),
            make_openai_response(content="The total is 1059000."),
        ]
    )
    agent = OpenAIToolAgent(registry, client, "gpt-4o-mini")
    result = harness.run(agent, "Total revenue?")
    assert result.status == "success"
    assert result.output == "The total is 1059000."
    assert [call.status for call in result.tool_calls] == ["ok", "argument_error"]
    assert result.llm_calls == 2
    assert result.input_tokens == 20 and result.output_tokens == 10
    first, second = client.requests
    assert first["tools"][0]["function"]["name"] == "query_database"
    roles = [message["role"] for message in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    assert second["messages"][3]["tool_call_id"] == "call_1"


def test_openai_invoke_requires_usage():
    client = FakeOpenAI(
        [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="x"))])]
    )
    with pytest.raises(ValueError):
        openai_invoke(client, "gpt-4o", [])()


def test_anthropic_invoke_collects_text_blocks():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="hello "),
                SimpleNamespace(type="tool_use"),
                SimpleNamespace(type="text", text="world"),
            ],
            usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    response = anthropic_invoke(
        client, "claude-3-5-haiku", [{"role": "user", "content": "hi"}], system="s"
    )()
    assert response.text == "hello world"
    assert (response.input_tokens, response.output_tokens) == (7, 3)
    assert captured["system"] == "s" and captured["max_tokens"] == 1024


def test_openai_completion_requests_json_mode():
    client = FakeOpenAI([make_openai_response(content='{"ok": true}')])
    assert openai_completion(client, "gpt-4o-mini")("sys", "usr") == '{"ok": true}'
    request = client.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0


def test_module_entry_point(tmp_path):
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "tracegate", "run", "Send alert email to admin"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "success"
