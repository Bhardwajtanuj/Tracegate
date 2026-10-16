from tracegate import AgentHarness, Settings
from tracegate.reference import ReferenceAgent


def main() -> None:
    settings = Settings(budget_usd=0.5, max_steps=10)
    with AgentHarness(settings) as harness:
        result = harness.run(
            ReferenceAgent(),
            "Query Q2 revenue and email the result to ops@example.com",
            session_id="quickstart",
        )
    print(result.status)
    print(result.output)
    print(f"cost=${result.cost_usd:.6f} tokens={result.input_tokens}+{result.output_tokens}")
    for call in result.tool_calls:
        print(call.name, call.status, f"{call.latency_s * 1000:.2f}ms")


if __name__ == "__main__":
    main()
