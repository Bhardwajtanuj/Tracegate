import os

from openai import OpenAI

from tracegate import AgentHarness, Settings
from tracegate.openai_agent import OpenAIToolAgent
from tracegate.reference import build_database, build_registry


def main() -> None:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tools = build_registry(build_database(), [])
    agent = OpenAIToolAgent(tools, client, model="gpt-4o-mini")
    settings = Settings(budget_usd=0.25, max_steps=8)
    with AgentHarness(settings) as harness:
        result = harness.run(agent, "What was total revenue in Q3?", session_id="openai-demo")
    print(result.status)
    print(result.output)
    print(f"cost=${result.cost_usd:.6f} tool_calls={len(result.tool_calls)}")


if __name__ == "__main__":
    main()
