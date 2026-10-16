from __future__ import annotations

import pytest

from tracegate import LoopDetected, LoopGuard, StepLimitExceeded


def test_identical_calls_are_detected_on_the_third_repeat():
    guard = LoopGuard(max_steps=10, repeats=3)
    guard.observe("search", {"q": "a"})
    guard.observe("search", {"q": "a"})
    with pytest.raises(LoopDetected) as caught:
        guard.observe("search", {"q": "a"})
    assert caught.value.period == 1


def test_argument_order_does_not_hide_a_loop():
    guard = LoopGuard(max_steps=10, repeats=2)
    guard.observe("t", {"a": 1, "b": 2})
    with pytest.raises(LoopDetected):
        guard.observe("t", {"b": 2, "a": 1})


def test_alternating_calls_are_detected_as_a_cycle():
    guard = LoopGuard(max_steps=20, repeats=3)
    for index in range(3):
        guard.observe("a", {})
        if index < 2:
            guard.observe("b", {})
    with pytest.raises(LoopDetected) as caught:
        guard.observe("b", {})
    assert caught.value.period == 2


def test_varied_calls_pass():
    guard = LoopGuard(max_steps=10, repeats=3)
    for index in range(8):
        guard.observe("t", {"n": index})
    assert guard.steps == 8


def test_step_limit():
    guard = LoopGuard(max_steps=2, repeats=3)
    guard.observe("t", {"n": 1})
    guard.observe("t", {"n": 2})
    with pytest.raises(StepLimitExceeded):
        guard.observe("t", {"n": 3})


def test_invalid_configuration():
    with pytest.raises(ValueError):
        LoopGuard(max_steps=0)
    with pytest.raises(ValueError):
        LoopGuard(max_steps=5, repeats=1)
