from __future__ import annotations

import json
from typing import Any

from tracegate.harness import RunContext
from tracegate.integrations import openai_invoke
from tracegate.tools import ToolRegistry


class OpenAIToolAgent:
    def __init__(
        self,
        tools: ToolRegistry,
        client: Any,
        model: str,
        system_prompt: str = "You are a precise assistant. Use the provided tools when needed.",
        temperature: float = 0.0,
    ):
        self.tools = tools
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature

    def run(self, ctx: RunContext, user_input: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        definitions = [{"type": "function", "function": schema} for schema in self.tools.schemas()]
        extra: dict[str, Any] = {"temperature": self.temperature}
        if definitions:
            extra["tools"] = definitions
        while True:
            response = ctx.llm(
                self.model,
                messages,
                openai_invoke(self.client, self.model, messages, **extra),
            )
            message = response.raw.choices[0].message
            calls = getattr(message, "tool_calls", None)
            if not calls:
                return message.content or ""
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in calls
                    ],
                }
            )
            for call in calls:
                try:
                    arguments: Any = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = call.function.arguments
                result = ctx.tool(call.function.name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.model_dump_json(),
                    }
                )
