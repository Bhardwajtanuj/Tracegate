from tracegate.evaluation.rag import RagSample, evaluate_rag, openai_ragas_models

SAMPLES = [
    RagSample(
        question="How do I roll back a deployment?",
        contexts=["Revert to the previous release tag, redeploy and verify the health checks."],
        answer="Revert to the previous release tag, redeploy and check service health.",
        reference="Revert to the previous release, redeploy and verify health checks.",
    ),
]


def main() -> None:
    llm, embeddings = openai_ragas_models()
    scores = evaluate_rag(SAMPLES, llm=llm, embeddings=embeddings)
    for name, value in sorted(scores.items()):
        print(f"{name}: {value:.3f}")


if __name__ == "__main__":
    main()
