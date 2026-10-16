from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

ToolStatus = Literal["ok", "argument_error", "unknown_tool", "tool_error"]


class ToolResult(BaseModel):
    status: ToolStatus
    output: Any = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any], Any]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def register(
        self, name: str, args_model: type[BaseModel], description: str = ""
    ) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        def decorator(handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
            self.add(
                ToolSpec(
                    name=name,
                    description=description,
                    args_model=args_model,
                    handler=handler,
                )
            )
            return handler

        return decorator

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._tools.values()]

    def execute(self, name: str, raw_arguments: Any) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                status="unknown_tool",
                errors=[{"type": "unknown_tool", "msg": f"no tool named {name!r}"}],
                retryable=True,
            )
        try:
            validated = spec.args_model.model_validate(raw_arguments)
        except ValidationError as exc:
            details = json.loads(
                exc.json(include_url=False, include_context=False, include_input=False)
            )
            return ToolResult(status="argument_error", errors=details, retryable=True)
        try:
            output = spec.handler(validated)
        except Exception as exc:
            return ToolResult(
                status="tool_error",
                errors=[{"type": type(exc).__name__, "msg": str(exc)}],
                retryable=False,
            )
        return ToolResult(status="ok", output=_jsonable(output))
