# tracegate

Observability and evaluation harness for tool-using agents.

tracegate wraps an agent run in an OpenTelemetry trace that uses OpenInference attribute names, enforces a cost budget, a loop guard and a tool-call limit, validates every tool call against a Pydantic schema, and scores the agent against a golden dataset with release gates that can fail a CI pipeline.

Complete technical documentation, including every source file, is in [docs/DOCUMENTATION.md](docs/DOCUMENTATION.md).

## Install

```
pip install -e ".[dev,otlp]"
```

Optional extras: `openai`, `anthropic`, `ragas`.

## Run the release gate

```
tracegate eval --dataset datasets/golden.jsonl --out reports
```

The command prints a scorecard, writes `scorecard.json`, `scorecard.md` and `results.jsonl` to the output directory, and exits with 1 when a blocking threshold fails.

## Trace a single run

```
tracegate run "Query Q2 revenue and email the result to ops@example.com" --spans
```

## Send traces to Phoenix

```
docker compose up -d
export TRACEGATE_OTLP_ENDPOINT=http://localhost:6006/v1/traces
tracegate eval
```

Open http://localhost:6006 to inspect the traces.

## Wrap your own agent

```python
from tracegate import AgentHarness, FunctionAgent, Settings, ToolRegistry

registry = ToolRegistry()

def run_agent(ctx, user_input):
    messages = [{"role": "user", "content": user_input}]
    reply = ctx.llm("gpt-4o-mini", messages, invoke)
    result = ctx.tool("lookup", {"id": 7})
    return reply.text

with AgentHarness(Settings()) as harness:
    result = harness.run(FunctionAgent(registry, run_agent), "question")
```

See `examples/` for runnable versions.

## Tests

```
make test
make lint
```
# Tracegate
