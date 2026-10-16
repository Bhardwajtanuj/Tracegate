from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from opentelemetry.trace import Status, StatusCode, Tracer

from tracegate.budget import BudgetExceeded, CostTracker
from tracegate.config import Settings
from tracegate.guardrails import LoopDetected, LoopGuard, StepLimitExceeded
from tracegate.models import LLMResponse, RunResult, RunStatus, extract_tool_calls
from tracegate.pricing import PriceBook
from tracegate.semconv import (
    INPUT_MIME,
    INPUT_VALUE,
    KIND_AGENT,
    KIND_LLM,
    KIND_TOOL,
    LLM_COST_USD,
    LLM_MODEL_NAME,
    LLM_TOKENS_COMPLETION,
    LLM_TOKENS_PROMPT,
    LLM_TOKENS_TOTAL,
    OUTPUT_MIME,
    OUTPUT_VALUE,
    RUN_COST_USD,
    RUN_LLM_CALLS,
    RUN_STATUS,
    RUN_TOOL_CALLS,
    SESSION_ID,
    SPAN_KIND,
    TOOL_NAME,
    TOOL_PARAMETERS,
    TOOL_RETRYABLE,
    TOOL_STATUS,
)
from tracegate.telemetry import Telemetry, to_record
from tracegate.tools import ToolRegistry, ToolResult

logger = logging.getLogger("tracegate")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _dump(value: Any, limit: int) -> str:
    return _clip(json.dumps(value, default=str, ensure_ascii=False), limit)


def _scalar(value: Any) -> str | bool | int | float:
    if isinstance(value, str | bool | int | float):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _classify(error: Exception) -> RunStatus:
    if isinstance(error, BudgetExceeded):
        return "budget_exceeded"
    if isinstance(error, LoopDetected):
        return "loop_detected"
    if isinstance(error, StepLimitExceeded):
        return "step_limit"
    return "error"


class RunContext:
    def __init__(
        self,
        tracer: Tracer,
        tools: ToolRegistry,
        cost: CostTracker,
        guard: LoopGuard,
        payload_limit: int,
        session_id: str | None = None,
    ):
        self.tracer = tracer
        self.tools = tools
        self.cost = cost
        self.guard = guard
        self.session_id = session_id
        self._payload_limit = payload_limit

    def llm(
        self,
        model: str,
        messages: list[dict[str, Any]],
        invoke: Callable[[], LLMResponse],
    ) -> LLMResponse:
        with self.tracer.start_as_current_span("llm.call") as span:
            span.set_attribute(SPAN_KIND, KIND_LLM)
            span.set_attribute(LLM_MODEL_NAME, model)
            span.set_attribute(INPUT_VALUE, _dump(messages, self._payload_limit))
            span.set_attribute(INPUT_MIME, "application/json")
            response = invoke()
            usage = self.cost.record(model, response.input_tokens, response.output_tokens)
            span.set_attribute(OUTPUT_VALUE, _clip(response.text, self._payload_limit))
            span.set_attribute(OUTPUT_MIME, "text/plain")
            span.set_attribute(LLM_TOKENS_PROMPT, usage.input_tokens)
            span.set_attribute(LLM_TOKENS_COMPLETION, usage.output_tokens)
            span.set_attribute(LLM_TOKENS_TOTAL, usage.input_tokens + usage.output_tokens)
            span.set_attribute(LLM_COST_USD, usage.cost_usd)
            self.cost.check()
            return response

    def tool(self, name: str, arguments: Any) -> ToolResult:
        with self.tracer.start_as_current_span(f"tool.{name}") as span:
            span.set_attribute(SPAN_KIND, KIND_TOOL)
            span.set_attribute(TOOL_NAME, name)
            span.set_attribute(TOOL_PARAMETERS, _dump(arguments, self._payload_limit))
            span.set_attribute(INPUT_VALUE, _dump(arguments, self._payload_limit))
            span.set_attribute(INPUT_MIME, "application/json")
            try:
                self.guard.observe(name, arguments)
            except (LoopDetected, StepLimitExceeded):
                span.set_attribute(TOOL_STATUS, "blocked")
                raise
            result = self.tools.execute(name, arguments)
            span.set_attribute(
                OUTPUT_VALUE, _dump(result.model_dump(mode="json"), self._payload_limit)
            )
            span.set_attribute(OUTPUT_MIME, "application/json")
            span.set_attribute(TOOL_STATUS, result.status)
            span.set_attribute(TOOL_RETRYABLE, result.retryable)
            if not result.ok:
                span.set_status(Status(StatusCode.ERROR, result.status))
            return result


class Agent(Protocol):
    tools: ToolRegistry

    def run(self, ctx: RunContext, user_input: str) -> str: ...


class FunctionAgent:
    def __init__(self, tools: ToolRegistry, fn: Callable[[RunContext, str], str]):
        self.tools = tools
        self._fn = fn

    def run(self, ctx: RunContext, user_input: str) -> str:
        return self._fn(ctx, user_input)


class RunSink(Protocol):
    def consider(self, result: RunResult) -> Any: ...


class AgentHarness:
    def __init__(
        self,
        settings: Settings | None = None,
        prices: PriceBook | None = None,
        telemetry: Telemetry | None = None,
        sink: RunSink | None = None,
    ):
        self.settings = settings or Settings()
        self.prices = prices or PriceBook.from_settings(self.settings)
        self.telemetry = telemetry or Telemetry(self.settings)
        self.sink = sink

    def __enter__(self) -> AgentHarness:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def flush(self) -> bool:
        return self.telemetry.flush()

    def close(self) -> None:
        self.telemetry.flush()
        self.telemetry.shutdown()

    def run(
        self,
        agent: Agent,
        user_input: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult:
        settings = self.settings
        limit = settings.max_payload_chars
        cost = CostTracker(settings.budget_usd, self.prices)
        guard = LoopGuard(settings.max_steps, settings.loop_repeats, settings.loop_max_period)
        context = RunContext(
            self.telemetry.tracer, agent.tools, cost, guard, limit, session_id=session_id
        )
        status: RunStatus = "success"
        output: str | None = None
        error: str | None = None
        started = time.perf_counter()
        with self.telemetry.tracer.start_as_current_span("agent.run") as span:
            trace_id = span.get_span_context().trace_id
            span.set_attribute(SPAN_KIND, KIND_AGENT)
            span.set_attribute(INPUT_VALUE, _clip(user_input, limit))
            if session_id:
                span.set_attribute(SESSION_ID, session_id)
            for key, value in (metadata or {}).items():
                span.set_attribute(f"metadata.{key}", _scalar(value))
            try:
                output = _text(agent.run(context, user_input))
            except Exception as exc:
                status = _classify(exc)
                error = f"{type(exc).__name__}: {exc}"
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, error))
            records = cost.records
            span.set_attribute(RUN_STATUS, status)
            span.set_attribute(RUN_COST_USD, cost.total_cost)
            span.set_attribute(RUN_LLM_CALLS, len(records))
            span.set_attribute(LLM_TOKENS_PROMPT, cost.input_tokens)
            span.set_attribute(LLM_TOKENS_COMPLETION, cost.output_tokens)
            span.set_attribute(LLM_TOKENS_TOTAL, cost.input_tokens + cost.output_tokens)
            span.set_attribute(RUN_TOOL_CALLS, guard.steps)
            if output is not None:
                span.set_attribute(OUTPUT_VALUE, _clip(output, limit))
        latency = time.perf_counter() - started
        spans = [to_record(raw) for raw in self.telemetry.collector.take(trace_id)]
        result = RunResult(
            trace_id=format(trace_id, "032x"),
            session_id=session_id,
            input=user_input,
            status=status,
            output=output,
            error=error,
            latency_s=latency,
            cost_usd=cost.total_cost,
            input_tokens=cost.input_tokens,
            output_tokens=cost.output_tokens,
            llm_calls=len(records),
            tool_calls=extract_tool_calls(spans),
            spans=spans,
            metadata=dict(metadata or {}),
        )
        if self.sink is not None:
            try:
                self.sink.consider(result)
            except Exception:
                logger.exception("run sink failed for trace %s", result.trace_id)
        return result
