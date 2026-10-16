from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

DEFAULT_METRICS = ("faithfulness", "response_relevancy", "context_precision", "context_recall")
REFERENCE_METRICS = {"context_precision", "context_recall"}
METRIC_CLASSES = {
    "faithfulness": "Faithfulness",
    "response_relevancy": "ResponseRelevancy",
    "context_precision": "LLMContextPrecisionWithReference",
    "context_recall": "LLMContextRecall",
}


class RagSample(BaseModel):
    question: str
    contexts: list[str]
    answer: str
    reference: str | None = None


def openai_ragas_models(
    model: str = "gpt-4o-mini", embedding_model: str = "text-embedding-3-small"
) -> tuple[Any, Any]:
    try:
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
    except ImportError as exc:
        raise RuntimeError(
            "RAG evaluation requires the extra: pip install 'tracegate[ragas]'"
        ) from exc
    return (
        LangchainLLMWrapper(ChatOpenAI(model=model)),
        LangchainEmbeddingsWrapper(OpenAIEmbeddings(model=embedding_model)),
    )


def _resolve(name: str) -> Any:
    from ragas import metrics

    class_name = METRIC_CLASSES.get(name)
    if class_name is None:
        raise ValueError(f"unknown metric {name!r}; choose from {sorted(METRIC_CLASSES)}")
    return getattr(metrics, class_name)()


def evaluate_rag(
    samples: Sequence[RagSample],
    llm: Any = None,
    embeddings: Any = None,
    metrics: Sequence[Any] = DEFAULT_METRICS,
) -> dict[str, float]:
    if not samples:
        raise ValueError("no samples to evaluate")
    requested = {metric for metric in metrics if isinstance(metric, str)}
    if requested & REFERENCE_METRICS and any(sample.reference is None for sample in samples):
        raise ValueError(
            "context_precision and context_recall require a reference for every sample"
        )
    try:
        from ragas import EvaluationDataset, SingleTurnSample, evaluate
    except ImportError as exc:
        raise RuntimeError(
            "RAG evaluation requires the extra: pip install 'tracegate[ragas]'"
        ) from exc
    resolved = [_resolve(metric) if isinstance(metric, str) else metric for metric in metrics]
    dataset = EvaluationDataset(
        samples=[
            SingleTurnSample(
                user_input=sample.question,
                retrieved_contexts=sample.contexts,
                response=sample.answer,
                reference=sample.reference,
            )
            for sample in samples
        ]
    )
    result = evaluate(
        dataset,
        metrics=resolved,
        llm=llm,
        embeddings=embeddings,
        show_progress=False,
        raise_exceptions=False,
    )
    scores: dict[str, list[float]] = {}
    for row in result.scores:
        for key, value in row.items():
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue
            scores.setdefault(key, []).append(float(value))
    if not scores:
        raise RuntimeError("ragas returned no valid scores")
    return {key: sum(values) / len(values) for key, values in scores.items()}
