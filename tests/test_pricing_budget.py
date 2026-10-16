from __future__ import annotations

import json

import pytest

from tracegate import BudgetExceeded, CostTracker, ModelPrice, PriceBook, UnknownModelError


def test_cost_is_computed_per_million_tokens():
    price = ModelPrice(input_per_million=2.0, output_per_million=8.0)
    assert price.cost(500_000, 250_000) == pytest.approx(3.0)


def test_prefix_match_prefers_longest_name():
    book = PriceBook()
    assert book.price_for("gpt-4o-mini-2024-07-18") == book.price_for("gpt-4o-mini")
    assert book.price_for("gpt-4o-2024-08-06") == book.price_for("gpt-4o")


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        PriceBook().cost("unlisted-model", 1, 1)


def test_price_file_overrides_defaults(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps({"custom": {"input_per_million": 1.0, "output_per_million": 1.0}}),
        encoding="utf-8",
    )
    book = PriceBook.from_file(path)
    assert book.cost("custom", 1_000_000, 0) == pytest.approx(1.0)
    assert book.cost("mock-llm", 1_000_000, 0) == pytest.approx(1.0)


def test_tracker_accumulates_and_breaks_the_circuit():
    tracker = CostTracker(0.01, PriceBook())
    tracker.record("gpt-4o", 1000, 100)
    tracker.check()
    assert tracker.total_cost == pytest.approx(0.0035)
    tracker.record("gpt-4o", 2000, 500)
    with pytest.raises(BudgetExceeded) as caught:
        tracker.check()
    assert caught.value.limit == 0.01
    assert caught.value.spent > 0.01


def test_tracker_breakdown_and_validation():
    tracker = CostTracker(1.0, PriceBook())
    tracker.record("gpt-4o", 10, 5)
    tracker.record("gpt-4o", 20, 5)
    tracker.record("mock-llm", 1, 1)
    summary = tracker.breakdown()
    assert summary["gpt-4o"]["calls"] == 2
    assert summary["gpt-4o"]["input_tokens"] == 30
    assert tracker.input_tokens == 31
    assert tracker.output_tokens == 11
    with pytest.raises(ValueError):
        tracker.record("gpt-4o", -1, 0)
