from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from tracegate.config import Settings


class ModelPrice(BaseModel):
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        total = input_tokens * self.input_per_million + output_tokens * self.output_per_million
        return total / 1_000_000


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "gpt-4o": ModelPrice(input_per_million=2.5, output_per_million=10.0),
    "gpt-4o-mini": ModelPrice(input_per_million=0.15, output_per_million=0.6),
    "claude-3-5-sonnet": ModelPrice(input_per_million=3.0, output_per_million=15.0),
    "claude-3-5-haiku": ModelPrice(input_per_million=0.8, output_per_million=4.0),
    "mock-llm": ModelPrice(input_per_million=1.0, output_per_million=2.0),
}


class UnknownModelError(Exception):
    def __init__(self, model: str):
        super().__init__(f"no price configured for model {model!r}")
        self.model = model


class PriceBook:
    def __init__(self, prices: dict[str, ModelPrice] | None = None):
        self._prices = dict(DEFAULT_PRICES if prices is None else prices)

    @classmethod
    def from_file(cls, path: str | Path) -> PriceBook:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        prices = dict(DEFAULT_PRICES)
        prices.update({name: ModelPrice.model_validate(entry) for name, entry in data.items()})
        return cls(prices)

    @classmethod
    def from_settings(cls, settings: Settings) -> PriceBook:
        if settings.pricing_file:
            return cls.from_file(settings.pricing_file)
        return cls()

    def price_for(self, model: str) -> ModelPrice:
        if model in self._prices:
            return self._prices[model]
        matches = [name for name in self._prices if model.startswith(name)]
        if not matches:
            raise UnknownModelError(model)
        return self._prices[max(matches, key=len)]

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return self.price_for(model).cost(input_tokens, output_tokens)
