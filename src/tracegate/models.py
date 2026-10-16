from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from tracegate.semconv import KIND_TOOL, TOOL_NAME, TOOL_PARAMETERS, TOOL_STATUS

RunStatus = Literal["success", "budget_exceeded", "loop_detected", "step_limit", "error"]


class LLMResponse(BaseModel):
    text: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    raw: Any = Field(default=None, exclude=True, repr=False)


class SpanRecord(BaseModel):
    trace_id: str
    span_id: str
    parent_id: str | None = None
    name: str
    kind: str
    status: str
    start_ns: int
    end_ns: int
    attributes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @property
    def latency_s(self) -> float:
        return (self.end_ns - self.start_ns) / 1e9


class ToolCall(BaseModel):
    name: str
    arguments: Any = None
    status: str
    latency_s: float


class RunResult(BaseModel):
    trace_id: str
    session_id: str | None = None
    input: str
    status: RunStatus
    output: str | None = None
    error: str | None = None
    latency_s: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    llm_calls: int
    tool_calls: list[ToolCall] = Field(default_factory=list)
    spans: list[SpanRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def extract_tool_calls(spans: list[SpanRecord]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for span in spans:
        if span.kind != KIND_TOOL:
            continue
        raw = span.attributes.get(TOOL_PARAMETERS)
        arguments: Any = raw
        if isinstance(raw, str):
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = raw
        calls.append(
            ToolCall(
                name=str(span.attributes.get(TOOL_NAME, span.name)),
                arguments=arguments,
                status=str(span.attributes.get(TOOL_STATUS, "error")),
                latency_s=span.latency_s,
            )
        )
    return calls
