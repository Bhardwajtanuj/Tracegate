from __future__ import annotations

import pytest

from tests import policies
from tracegate import AgentHarness, FunctionAgent, LLMResponse, Settings, ToolRegistry
from tracegate.reference import ReferenceAgent


def run(harness: AgentHarness, prompt: str, policy=policies.keyword_policy):
    return harness.run(ReferenceAgent(policy=policy), prompt, session_id="s-1")


def test_successful_run_produces_a_connected_trace(harness):
    result = run(harness, "Fetch revenue numbers for Q3")
    assert result.status == "success"
    assert "412000" in result.output
    assert result.llm_calls == 2
    assert [call.name for call in result.tool_calls] == ["query_database"]
    assert result.cost_usd > 0
    assert result.input_tokens > 0 and result.output_tokens > 0
    kinds = [span.kind for span in result.spans]
    assert kinds.count("AGENT") == 1
    assert kinds.count("LLM") == 2
    assert kinds.count("TOOL") == 1
    root = next(span for span in result.spans if span.kind == "AGENT")
    assert root.parent_id is None
    assert all(span.trace_id == result.trace_id for span in result.spans)
    children = [span for span in result.spans if span.kind != "AGENT"]
    assert all(span.parent_id == root.span_id for span in children)
    assert root.attributes["session.id"] == "s-1"
    assert root.attributes["agent.run.status"] == "success"
    assert root.attributes["agent.run.llm_calls"] == 2


def test_llm_spans_follow_openinference_attributes(harness):
    result = run(harness, "Fetch revenue numbers for Q3")
    llm = next(span for span in result.spans if span.kind == "LLM")
    assert llm.attributes["llm.model_name"] == "mock-llm"
    assert llm.attributes["llm.token_count.total"] == (
        llm.attributes["llm.token_count.prompt"] + llm.attributes["llm.token_count.completion"]
    )
    tool = next(span for span in result.spans if span.kind == "TOOL")
    assert tool.attributes["tool.name"] == "query_database"
    assert tool.attributes["agent.tool.status"] == "ok"


def test_loop_is_stopped_and_reported(harness):
    result = run(harness, "anything", policies.looping_policy)
    assert result.status == "loop_detected"
    assert result.error and "cycle" in result.error
    assert [call.status for call in result.tool_calls] == ["ok", "ok", "blocked"]
    assert result.spans[0].status == "ERROR"


def test_alternating_loop_is_detected(harness):
    result = run(harness, "anything", policies.alternating_policy)
    assert result.status == "loop_detected"
    assert len(result.tool_calls) == 6


def test_step_limit_stops_runs_that_vary_their_calls():
    harness = AgentHarness(Settings(_env_file=None, max_steps=4))
    try:
        result = run(harness, "anything", policies.drifting_policy)
    finally:
        harness.close()
    assert result.status == "step_limit"
    assert len(result.tool_calls) == 5
    assert result.tool_calls[-1].status == "blocked"


def test_budget_circuit_breaker():
    harness = AgentHarness(Settings(_env_file=None, budget_usd=0.000001))
    try:
        result = run(harness, "Fetch revenue numbers for Q3")
    finally:
        harness.close()
    assert result.status == "budget_exceeded"
    assert result.cost_usd > 0.000001
    assert result.tool_calls == []


def test_agent_exceptions_never_escape(harness):
    result = run(harness, "anything", policies.crashing_policy)
    assert result.status == "error"
    assert "policy exploded" in result.error
    assert result.output is None


def test_argument_errors_are_recorded_and_recoverable(harness):
    result = run(harness, "anything", policies.fumbling_policy)
    assert result.status == "success"
    assert [call.status for call in result.tool_calls] == ["argument_error", "ok"]
    failed = next(span for span in result.spans if span.kind == "TOOL")
    assert failed.status == "ERROR"


def test_unknown_tools_are_reported(harness):
    result = run(harness, "anything", policies.unknown_tool_policy)
    assert result.status == "success"
    assert result.tool_calls[0].status == "unknown_tool"


def test_unpriced_model_fails_the_run_loudly(harness):
    agent = ReferenceAgent(model="mystery-model")
    result = harness.run(agent, "Fetch revenue numbers for Q3")
    assert result.status == "error"
    assert "mystery-model" in result.error


def test_function_agent_and_metadata(harness):
    registry = ToolRegistry()

    def fn(ctx, text):
        response = ctx.llm(
            "mock-llm",
            [{"role": "user", "content": text}],
            lambda: LLMResponse(text=text.upper(), input_tokens=3, output_tokens=2),
        )
        return response.text

    result = harness.run(
        FunctionAgent(registry, fn),
        "hello",
        metadata={"tenant": "acme", "attempt": 2, "tags": ["a"]},
    )
    assert result.output == "HELLO"
    root = next(span for span in result.spans if span.kind == "AGENT")
    assert root.attributes["metadata.tenant"] == "acme"
    assert root.attributes["metadata.attempt"] == 2
    assert root.attributes["metadata.tags"] == '["a"]'


def test_non_string_agent_output_is_serialized(harness):
    agent = FunctionAgent(ToolRegistry(), lambda ctx, text: {"answer": 1})
    assert harness.run(agent, "x").output == '{"answer": 1}'


def test_payloads_are_clipped():
    harness = AgentHarness(Settings(_env_file=None, max_payload_chars=64))
    try:
        agent = FunctionAgent(ToolRegistry(), lambda ctx, text: text)
        result = harness.run(agent, "x" * 500)
    finally:
        harness.close()
    root = result.spans[0]
    assert len(root.attributes["input.value"]) == 64
    assert root.attributes["input.value"].endswith("...")


def test_concurrent_runs_keep_traces_isolated(harness):
    from concurrent.futures import ThreadPoolExecutor

    prompts = ["Fetch revenue numbers for Q3", "Send alert email to admin"] * 8
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda prompt: run(harness, prompt), prompts))
    assert len({result.trace_id for result in results}) == len(prompts)
    for result in results:
        assert all(span.trace_id == result.trace_id for span in result.spans)
        assert len([span for span in result.spans if span.kind == "TOOL"]) == 1
    assert [result.tool_calls[0].name for result in results[:2]] == [
        "query_database",
        "send_email",
    ]


def test_collector_does_not_retain_finished_traces(harness):
    run(harness, "Fetch revenue numbers for Q3")
    assert harness.telemetry.collector._spans == {}


def test_failing_sink_does_not_break_the_run(settings):
    class BrokenSink:
        def consider(self, result):
            raise OSError("disk full")

    harness = AgentHarness(settings, sink=BrokenSink())
    try:
        result = run(harness, "Fetch revenue numbers for Q3")
    finally:
        harness.close()
    assert result.status == "success"


def test_console_exporter_writes_to_stderr(capsys):
    harness = AgentHarness(Settings(_env_file=None, console_export=True))
    try:
        run(harness, "Send alert email to admin")
        harness.flush()
    finally:
        harness.close()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "openinference.span.kind" in captured.err


def test_otlp_headers_parse_and_reject_garbage():
    settings = Settings(_env_file=None, otlp_headers="Authorization=Basic abc==, x-key=1")
    assert settings.otlp_header_map == {"Authorization": "Basic abc==", "x-key": "1"}
    broken = Settings(_env_file=None, otlp_headers="broken")
    with pytest.raises(ValueError):
        _ = broken.otlp_header_map


def test_blank_endpoint_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("TRACEGATE_OTLP_ENDPOINT", "")
    assert Settings(_env_file=None).otlp_endpoint is None


def test_otlp_export_reaches_a_collector_with_headers():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "type": self.headers.get("Content-Type"),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        _env_file=None,
        otlp_endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
        otlp_headers="Authorization=Basic dGVzdA==",
    )
    harness = AgentHarness(settings)
    try:
        run(harness, "Send alert email to admin")
        assert harness.flush()
    finally:
        harness.close()
        server.shutdown()
        server.server_close()
    assert received
    first = received[0]
    assert first["path"] == "/v1/traces"
    assert first["auth"] == "Basic dGVzdA=="
    assert first["type"] == "application/x-protobuf"
    assert b"agent.run" in first["body"]
    assert b"tool.send_email" in first["body"]
    assert b"openinference.span.kind" in first["body"]
