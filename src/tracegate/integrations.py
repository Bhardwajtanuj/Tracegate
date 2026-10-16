from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tracegate.models import LLMResponse


def openai_invoke(
    client: Any, model: str, messages: list[dict[str, Any]], **kwargs: Any
) -> Callable[[], LLMResponse]:
    def invoke() -> LLMResponse:
        response = client.chat.completions.create(model=model, messages=messages, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is None:
            raise ValueError("the provider response did not include token usage")
        return LLMResponse(
            text=response.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            raw=response,
        )

    return invoke


def anthropic_invoke(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 1024,
    system: str | None = None,
    **kwargs: Any,
) -> Callable[[], LLMResponse]:
    def invoke() -> LLMResponse:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            **kwargs,
        }
        if system is not None:
            params["system"] = system
        response = client.messages.create(**params)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return LLMResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            raw=response,
        )

    return invoke


def openai_completion(client: Any, model: str) -> Callable[[str, str], str]:
    def complete(system: str, user: str) -> str:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""

    return complete
