import json

from pydantic import BaseModel, ConfigDict, Field

from tracegate import AgentHarness, FunctionAgent, LLMResponse, RunContext, Settings, ToolRegistry


class ConvertCurrency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float = Field(gt=0)
    source: str = Field(pattern=r"^[A-Z]{3}$")
    target: str = Field(pattern=r"^[A-Z]{3}$")


RATES = {("USD", "EUR"): 0.92, ("EUR", "USD"): 1.09}

registry = ToolRegistry()


@registry.register("convert_currency", ConvertCurrency, "Convert an amount between currencies")
def convert_currency(args: ConvertCurrency) -> dict:
    rate = RATES[(args.source, args.target)]
    return {"converted": round(args.amount * rate, 2), "rate": rate}


def plan(messages: list[dict]) -> LLMResponse:
    observed = [message for message in messages if message["role"] == "tool"]
    if observed:
        payload = json.loads(observed[-1]["content"])
        text = json.dumps({"final": f"Converted amount: {payload['output']['converted']}"})
    else:
        arguments = {"amount": 250, "source": "USD", "target": "EUR"}
        text = json.dumps({"tool": "convert_currency", "arguments": arguments})
    return LLMResponse(text=text, input_tokens=len(json.dumps(messages)) // 4, output_tokens=24)


def run_agent(ctx: RunContext, user_input: str) -> str:
    messages = [{"role": "user", "content": user_input}]
    while True:
        reply = ctx.llm("mock-llm", messages, lambda: plan(messages))
        action = json.loads(reply.text)
        if "final" in action:
            return action["final"]
        result = ctx.tool(action["tool"], action["arguments"])
        messages.append({"role": "tool", "content": result.model_dump_json()})


def main() -> None:
    with AgentHarness(Settings()) as harness:
        result = harness.run(FunctionAgent(registry, run_agent), "Convert 250 USD to EUR")
    print(result.status, result.output)
    print([span.name for span in result.spans])


if __name__ == "__main__":
    main()
