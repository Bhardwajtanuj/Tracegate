from __future__ import annotations

import threading

from pydantic import BaseModel

from tracegate.pricing import PriceBook


class BudgetExceeded(Exception):
    def __init__(self, spent: float, limit: float):
        super().__init__(f"budget exceeded: spent ${spent:.6f} of ${limit:.6f}")
        self.spent = spent
        self.limit = limit


class UsageRecord(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CostTracker:
    def __init__(self, limit_usd: float, prices: PriceBook):
        self.limit_usd = limit_usd
        self._prices = prices
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()

    def record(self, model: str, input_tokens: int, output_tokens: int) -> UsageRecord:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        usage = UsageRecord(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self._prices.cost(model, input_tokens, output_tokens),
        )
        with self._lock:
            self._records.append(usage)
        return usage

    def check(self) -> None:
        spent = self.total_cost
        if spent > self.limit_usd:
            raise BudgetExceeded(spent, self.limit_usd)

    @property
    def records(self) -> list[UsageRecord]:
        with self._lock:
            return list(self._records)

    @property
    def total_cost(self) -> float:
        return sum(record.cost_usd for record in self.records)

    @property
    def input_tokens(self) -> int:
        return sum(record.input_tokens for record in self.records)

    @property
    def output_tokens(self) -> int:
        return sum(record.output_tokens for record in self.records)

    def breakdown(self) -> dict[str, dict[str, float]]:
        summary: dict[str, dict[str, float]] = {}
        for record in self.records:
            entry = summary.setdefault(
                record.model,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            )
            entry["calls"] += 1
            entry["input_tokens"] += record.input_tokens
            entry["output_tokens"] += record.output_tokens
            entry["cost_usd"] += record.cost_usd
        return summary
