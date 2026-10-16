# tracegate

Observability, runtime protection and release gating for tool-using agents.

Agents fail in ways a normal service does not. They call the same tool forever, send malformed arguments, let the context grow until one run costs far more than planned, and return an answer that looks right after a wrong path. The final string hides all of it.

tracegate wraps an agent run, records the whole path as an OpenTelemetry trace, stops the run when it misbehaves, and scores the agent against a golden dataset so a pull request can be blocked before a regression ships.

## Features

- **Tracing.** Every run produces `agent.run`, `llm.call` and `tool.<name>` spans with OpenInference attribute names. Export over OTLP/HTTP to Arize Phoenix, Langfuse or any collector.
- **Cost budget.** Token usage is priced per model on every call. The run stops with `budget_exceeded` once the limit is crossed.
- **Loop and step guard.** Repeating tool-call patterns (period 1 to 3 by default) and runaway call counts are blocked before the tool executes.
- **Validated tools.** Each tool declares a Pydantic model. Bad arguments come back to the model as a short structured error instead of an exception.
- **Failure containment.** A run never raises. Budget, loop, step limit and agent crashes become a status on the result.
- **Evaluation.** Golden dataset in JSON Lines, per-case metrics read from the recorded trace, scorecard with blocking and warning thresholds, exit code 1 on failure.
- **Optional judge and Ragas.** A model-based trajectory review and Ragas scoring for retrieval answers.
- **Feedback loop.** Runs that look wrong are captured for review and promoted into the golden dataset.
- **Provider neutral.** The test suite and the bundled reference agent need no network or API key. OpenAI function calling and Anthropic adapters are included.

## How it fits together

```
AgentHarness.run(agent, input)
  |-- root span  agent.run
  |-- ctx.llm(...)    -> span llm.call      cost recorded, budget checked
  |-- ctx.tool(...)   -> span tool.<name>   loop guard, schema validation
  v
RunResult  status, output, cost, tokens, tool calls, spans
  |-- OTLP exporter -> Phoenix / Langfuse
  |-- CaptureSink   -> candidates for the golden dataset

EvaluationRunner -> metrics -> Scorecard -> thresholds -> pass / warn / fail
```

## Install

From a clone of the repository:

```
pip install -e ".[dev,otlp]"
```

Python 3.10 or newer. Extras: `otlp` (trace export), `openai`, `anthropic`, `ragas`, `dev`.

## Quick start

Run the release gate against the bundled dataset and reference agent:

```
tracegate eval --dataset datasets/golden.jsonl --out reports
```

Example output:

```
# Agent evaluation scorecard

Verdict: **PASS**

Cases: 8, passed: 8

| Metric | Target | Actual | Result |
| --- | --- | --- | --- |
| tool_selection_accuracy | >= 0.92 | 1.0000 | pass |
| schema_alignment | >= 0.92 | 1.0000 | pass |
| unhandled_error_rate | <= 0 | 0.0000 | pass |
| loop_rate | <= 0 | 0.0000 | pass |
| budget_breach_rate | <= 0 | 0.0000 | pass |
| latency_p95_s | <= 12 | 0.0048 | pass |
```

`scorecard.json`, `scorecard.md` and `results.jsonl` (full traces per case) are written to the output directory. The command exits with 1 when a blocking threshold fails, 0 otherwise, and 2 on a configuration error.

Trace a single prompt and print the spans:

```
tracegate run "Query Q2 revenue and email the result to ops@example.com" --spans
```

The run produces this span tree:

```
agent.run
  llm.call
  tool.query_database
  llm.call
  tool.send_email
  llm.call
```

## Wrap your agent

Register tools with a Pydantic argument model, then give the harness an agent. `OpenAIToolAgent` implements the Chat Completions tool-calling loop:

```python
from openai import OpenAI
from pydantic import BaseModel, Field

from tracegate import AgentHarness, Settings, ToolRegistry
from tracegate.openai_agent import OpenAIToolAgent


class LookupOrder(BaseModel):
    order_id: int = Field(gt=0)


tools = ToolRegistry()


@tools.register("lookup_order", LookupOrder, "Fetch an order by id")
def lookup_order(args: LookupOrder) -> dict:
    return {"order_id": args.order_id, "status": "shipped"}


agent = OpenAIToolAgent(tools, OpenAI(), model="gpt-4o-mini")

with AgentHarness(Settings(budget_usd=0.25, max_steps=8)) as harness:
    result = harness.run(agent, "Where is order 42?", session_id="demo")

print(result.status, result.output, result.cost_usd)
```

For any other framework, implement `run(ctx, user_input)` and route model calls through `ctx.llm` and tool calls through `ctx.tool`. `FunctionAgent` adapts a plain function. See `examples/custom_agent.py`.

## Send traces to a backend

Arize Phoenix, with the bundled compose file:

```
docker compose up -d
export TRACEGATE_OTLP_ENDPOINT=http://localhost:6006/v1/traces
tracegate eval
```

Langfuse:

```
AUTH=$(printf '%s' 'PUBLIC_KEY:SECRET_KEY' | base64)
export TRACEGATE_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel/v1/traces
export TRACEGATE_OTLP_HEADERS="Authorization=Basic ${AUTH}"
```

## Gate releases in CI

`.github/workflows/ci.yml` runs lint and tests on Python 3.10 to 3.12, then runs `tracegate eval`, writes the scorecard to the job summary and uploads the reports. To gate your own agent:

```
tracegate eval --agent my_package.agent:create_agent --dataset evals/golden.jsonl
```

Thresholds are configurable with `--thresholds path.json`. Defaults:

| Metric | Target | Severity |
| --- | --- | --- |
| tool_selection_accuracy | >= 0.92 | block |
| schema_alignment | >= 0.92 | block |
| unhandled_error_rate | 0 | block |
| loop_rate | 0 | block |
| budget_breach_rate | 0 | block |
| call_limit_compliance | 1 | block |
| answer_match_rate | >= 0.9 | block |
| latency_p95_s | <= 12 | warn |
| mean_trajectory_efficiency | >= 0.7 | warn |
| mean_cost_usd | <= 0.25 | warn |

## Grow the golden dataset from production

```python
from tracegate import AgentHarness, CaptureSink, Settings

settings = Settings()
harness = AgentHarness(settings, sink=CaptureSink("candidates.jsonl", settings))
```

Runs that fail, hit argument errors, or approach the budget or step limit are written to `candidates.jsonl`. Set `expected_tools` on the ones worth keeping, then:

```
tracegate promote --candidates candidates.jsonl --dataset datasets/golden.jsonl
```

## Commands

| Command | Purpose |
| --- | --- |
| `tracegate eval` | Run a golden dataset and gate on the scorecard |
| `tracegate run PROMPT` | Run one prompt and print the traced result |
| `tracegate promote` | Move reviewed candidates into the dataset |

`python -m tracegate` is equivalent.

## Configuration

Environment variables with the prefix `TRACEGATE_`, a `.env` file, or `Settings(...)` arguments. See `.env.example`.

| Variable | Default | Meaning |
| --- | --- | --- |
| TRACEGATE_OTLP_ENDPOINT | unset | OTLP/HTTP traces URL. Unset disables export. |
| TRACEGATE_OTLP_HEADERS | empty | Comma separated `key=value` exporter headers |
| TRACEGATE_PROJECT_NAME | tracegate | Project name shown in Phoenix |
| TRACEGATE_BUDGET_USD | 2.0 | Cost limit per run |
| TRACEGATE_MAX_STEPS | 25 | Tool calls allowed per run |
| TRACEGATE_LOOP_REPEATS | 3 | Repetitions that count as a loop |
| TRACEGATE_PRICING_FILE | unset | JSON file with model prices |
| TRACEGATE_CONSOLE_EXPORT | false | Print spans to standard error |

The built-in price table is a reference. Provide current prices for the models you run through a pricing file (see `examples/prices.json`). A model without a price fails the run on purpose, because a silent zero would disable the budget.

## Project layout

```
src/tracegate/            harness, tracing, budget, guards, tools, CLI
src/tracegate/evaluation/ dataset, metrics, scorecard, judge, runner, Ragas
datasets/golden.jsonl     bundled golden cases
examples/                 runnable examples, thresholds and prices files
tests/                    pytest suite
docs/DOCUMENTATION.md     full technical documentation and source listing
```

## Development

```
make install
make lint
make test
```

The suite runs offline. The Ragas tests are skipped unless the `ragas` extra is installed.

## Documentation

The complete design, trace model, metric definitions, algorithms and source listing are in [docs/DOCUMENTATION.md](docs/DOCUMENTATION.md).

## Notes and limits

- The budget is checked after each model call, so the call that crosses the limit is still billed.
- Loop detection compares exact tool names and arguments. An agent that varies its arguments every time is stopped by the step limit.
- The reference agent is a deterministic stand-in for a model so the pipeline can be tested without a network.
- The Ragas extra pins `ragas<0.5` and compatible LangChain packages because Ragas changes its interface between releases.
- The interface is synchronous.
