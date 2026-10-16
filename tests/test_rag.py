from __future__ import annotations

import pytest

from tracegate.evaluation.rag import RagSample, evaluate_rag

SAMPLES = [
    RagSample(
        question="What was Q3 revenue?",
        contexts=["Q3 revenue was 412000"],
        answer="Revenue was 412000",
        reference="412000",
    ),
    RagSample(
        question="What was Q2 revenue?",
        contexts=["Q2 revenue was 347000"],
        answer="Revenue is unknown",
        reference="347000",
    ),
]


def test_reference_metrics_require_references():
    sample = RagSample(question="q", contexts=["c"], answer="a")
    with pytest.raises(ValueError, match="reference"):
        evaluate_rag([sample])


def test_empty_input_and_unknown_metric():
    with pytest.raises(ValueError):
        evaluate_rag([])
    ragas = pytest.importorskip("ragas")
    assert ragas
    with pytest.raises(ValueError, match="unknown metric"):
        evaluate_rag(SAMPLES, metrics=["nonsense"])


def test_scores_are_averaged_across_samples():
    pytest.importorskip("ragas")
    from ragas.metrics import StringPresence

    scores = evaluate_rag(SAMPLES, metrics=[StringPresence()])
    assert scores == {"string_present": 0.5}
