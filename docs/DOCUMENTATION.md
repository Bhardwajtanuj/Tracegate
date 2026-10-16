# tracegate: technical documentation

tracegate is a Python library and command line tool that adds observability, runtime protection and release evaluation to tool-using agents. This document covers the design, the data model, the algorithms, configuration, usage and the complete source of the project.

## Contents

1. Purpose and scope
2. Architecture
3. Run lifecycle
4. Trace model
5. Runtime controls
6. Evaluation
7. Installation and configuration
8. Usage
9. Repository layout
10. Source code
11. Tests
12. Operating notes and limits
13. Verification record

## 1. Purpose and scope

Agents fail differently from ordinary services. They keep calling the same tool with the same arguments, they send malformed arguments to tools, they grow their context until a single request costs far more than planned, and they return an answer that looks fine while the path that produced it was wrong. The final string alone does not show any of this.

tracegate addresses it with four parts.

- A harness that runs any agent inside an OpenTelemetry trace. Spans use the OpenInference attribute names, so the traces display correctly in Arize Phoenix and can be sent to Langfuse or any OTLP/HTTP collector.
- Runtime controls that stop a run: a per-run cost budget, a repeated-call detector, a hard limit on tool calls, and schema validation of every tool call.
- An evaluation runner that executes a golden dataset, derives per-case metrics from the recorded trace, aggregates them into a scorecard and compares the scorecard against thresholds. A failing blocking threshold produces a non-zero exit code so that a CI job can stop a release.
- A feedback path that records production runs that look wrong and turns reviewed ones into new golden cases.

The package has no dependency on a specific model provider. Provider adapters for OpenAI and Anthropic are optional and small. The test suite and the bundled reference agent run without network access or API keys.

## 2. Architecture

```
caller
  |
  v
AgentHarness.run(agent, input)
  |   creates per-run CostTracker and LoopGuard
  |   opens root span "agent.run"
  v
Agent.run(ctx, input)                       your code, or OpenAIToolAgent, or ReferenceAgent
  |
  |-- ctx.llm(model, messages, invoke)      span "llm.call", cost recorded, budget checked
  |-- ctx.tool(name, arguments)             span "tool.<name>", loop guard, schema validation
  v
RunResult  (status, output, cost, tokens, tool calls, spans)
  |
  +--> TraceCollector   keeps finished spans per trace id for evaluation
  +--> BatchSpanProcessor -> OTLP/HTTP exporter -> Phoenix, Langfuse or other collector
  +--> RunSink          optional, flags runs for review (CaptureSink)

EvaluationRunner
  for each GoldenCase: new agent -> harness.run -> compute_metrics -> optional judge
  aggregate -> Scorecard -> thresholds -> verdict -> reports and exit code
```

The harness owns no agent logic. An agent is any object with a `tools` attribute (a `ToolRegistry`) and a `run(ctx, user_input)` method. All instrumentation goes through the `RunContext` that the harness passes in, so an agent written against the context is traced, budgeted and guarded without further code.

Each harness has its own `TracerProvider`. It does not touch the global OpenTelemetry provider, so it can coexist with other instrumentation in the same process.

## 3. Run lifecycle

`AgentHarness.run` performs these steps.

1. A `CostTracker` with the configured budget and a `LoopGuard` with the configured limits are created for this run only.
2. The root span `agent.run` is opened. Input, session id and metadata are written to it.
3. `agent.run(context, user_input)` is called. Inside it, `ctx.llm` and `ctx.tool` create child spans.
4. Any exception raised by the agent, by a guard or by the budget check is caught and mapped to a run status. Nothing escapes `run`.
5. Run totals are written to the root span and the root span ends.
6. The finished spans for this trace are taken from the collector, converted to `SpanRecord` objects and attached to the `RunResult` together with the tool calls extracted from them.
7. If a sink is configured, it receives the result. A failing sink is logged and ignored so that it cannot break the run.

`harness.run` may be called from several threads at once. Each run has its own tracker, guard and trace, and the collector keys spans by trace id.

### Run statuses

| Status | Cause |
| --- | --- |
| success | The agent returned. |
| budget_exceeded | Accumulated cost exceeded the budget after a model call. |
| loop_detected | A repeating pattern of identical tool calls was detected. |
| step_limit | The agent attempted more tool calls than `max_steps`. |
| error | Any other exception, including an agent bug or a model with no configured price. |

## 4. Trace model

Three span names are used. All spans of a run share one trace id and the `llm.call` and `tool.<name>` spans are children of `agent.run`.

### agent.run

| Attribute | Content |
| --- | --- |
| openinference.span.kind | AGENT |
| input.value | User input, clipped to `max_payload_chars` |
| output.value | Final output, clipped, set only on success |
| session.id | Present when a session id is passed to `run` |
| metadata.<key> | One attribute per metadata entry. Non-scalar values are JSON encoded. |
| agent.run.status | One of the run statuses above |
| agent.run.cost_usd | Total cost of the run |
| agent.run.llm_calls | Number of model calls |
| agent.run.tool_calls | Number of tool calls seen by the guard, including a blocked one |
| llm.token_count.prompt, .completion, .total | Token totals for the run |

The span status is ERROR with the error text when the run did not succeed, and the exception is recorded as a span event.

### llm.call

| Attribute | Content |
| --- | --- |
| openinference.span.kind | LLM |
| llm.model_name | Model name given to `ctx.llm` |
| input.value, input.mime_type | JSON encoded messages, `application/json` |
| output.value, output.mime_type | Response text, `text/plain` |
| llm.token_count.prompt, .completion, .total | Tokens reported by the provider |
| agent.llm.cost_usd | Cost of this call |

### tool.<name>

| Attribute | Content |
| --- | --- |
| openinference.span.kind | TOOL |
| tool.name, tool.parameters | Tool name and JSON encoded arguments |
| input.value, input.mime_type | Same arguments, `application/json` |
| output.value, output.mime_type | JSON encoded `ToolResult`, `application/json` |
| agent.tool.status | ok, argument_error, unknown_tool, tool_error or blocked |
| agent.tool.retryable | Whether the failure is worth retrying |

The span status is ERROR for any status other than ok. A blocked call is one rejected by the loop guard or the step limit. It has the status `blocked`, the exception is recorded on the span, and the tool handler is never executed.

### Resource attributes

`service.name` comes from `TRACEGATE_SERVICE_NAME`. `openinference.project.name` comes from `TRACEGATE_PROJECT_NAME`, which Phoenix uses to group traces into a project.

### SpanRecord and RunResult

Evaluation does not work with OpenTelemetry objects. Each finished span is converted into a `SpanRecord` (trace id, span id, parent id, name, kind, status, start and end in nanoseconds, attributes, error text). `RunResult.tool_calls` is derived from the TOOL spans. Both are plain Pydantic models and serialize to JSON, which is how `results.jsonl` is produced.

## 5. Runtime controls

### 5.1 Cost budget

`ctx.llm(model, messages, invoke)` calls `invoke()`, which must return an `LLMResponse` with the response text and the input and output token counts reported by the provider. The harness then

1. records the usage with the `CostTracker`, which prices it through the `PriceBook`;
2. writes the token and cost attributes to the span;
3. calls `CostTracker.check()`, which raises `BudgetExceeded` when the total cost of the run is strictly greater than the budget.

The check runs after the call, so the call that crosses the limit has already been paid for. The run then stops with status `budget_exceeded`, which prevents every later call.

Prices are expressed per million tokens, separately for input and output. A model name matches a price entry exactly, or by the longest entry that is a prefix of the name, so `gpt-4o-mini-2024-07-18` resolves to `gpt-4o-mini` and not to `gpt-4o`. A model with no matching entry raises `UnknownModelError` and the run ends with status `error`. A silent price of zero would defeat the budget, so there is no default price.

The built-in table is a reference. Supply current rates for the models you use through a JSON file and `TRACEGATE_PRICING_FILE`. Entries in the file override the built-in ones.

### 5.2 Loop and step guard

`ctx.tool` calls `LoopGuard.observe(name, arguments)` before the tool runs. The guard keeps the history of calls as pairs of tool name and canonical JSON of the arguments (keys sorted, so key order does not matter).

Step limit: when the history already holds `max_steps` calls, the next call raises `StepLimitExceeded`. It is not added to the history.

Cycle detection: after a call is appended, the guard checks each period `p` from 1 to `loop_max_period`. It takes the last `p * loop_repeats` entries and tests whether they are the first `p` entries repeated `loop_repeats` times. If so, `LoopDetected` is raised. The call that completes the pattern is the one rejected.

With the defaults (`loop_repeats` 3, `loop_max_period` 3):

- A, A, A is a loop of period 1. The third A is blocked.
- A, B, A, B, A, B is a loop of period 2. The last B is blocked.
- An agent that changes its arguments on every call never matches a cycle, and is stopped by the step limit instead.

Calls whose arguments fail validation count as steps. A model that keeps sending the same invalid arguments is therefore stopped by the loop detector.

### 5.3 Tool validation

Tools are registered in a `ToolRegistry` together with a Pydantic model describing their arguments.

```python
registry = ToolRegistry()

@registry.register("query_database", DatabaseQuery, "Run a read-only SQL SELECT")
def query_database(args: DatabaseQuery) -> dict:
    ...
```

`ToolRegistry.execute` never raises. It returns a `ToolResult` with one of four statuses.

| Status | Meaning | retryable |
| --- | --- | --- |
| ok | Arguments validated and the handler returned. | false |
| argument_error | Arguments failed Pydantic validation. `errors` holds the structured validation errors without input echo, URLs or stack traces. | true |
| unknown_tool | No tool with that name. | true |
| tool_error | The handler raised. `errors` holds the exception type and message. No traceback is returned. | false |

The `ToolResult` is what goes back to the model. Because the errors are structured and short, a model can usually correct its arguments on the next turn. `ToolRegistry.schemas()` returns each tool as a name, description and JSON schema, which is the shape expected by function-calling APIs.

The bundled `query_database` tool shows the layered approach: the argument model rejects anything but a single SELECT statement and the connection itself is opened with `PRAGMA query_only = ON`, so a write fails even if validation were bypassed.

### 5.4 Failure containment

The harness converts every failure into a status on the `RunResult`. An evaluation over many cases therefore always completes, and a crashing agent shows up as `unhandled_error_rate` in the scorecard instead of aborting the job.

## 6. Evaluation

### 6.1 Golden dataset

A dataset is a JSON Lines file. Each line is one case.

| Field | Type | Meaning |
| --- | --- | --- |
| id | string | Unique case id. Duplicates are rejected. |
| input | string | User request sent to the agent. |
| expected_tools | list of strings | Tools the agent should use. Empty means no tool should be used. |
| max_tool_calls | integer | Most tool calls allowed for the case. Default 5. |
| expected_substrings | list of strings | Text that must appear in the final answer, compared case-insensitively. |
| tags | list of strings | Free labels. |

Errors in the file are reported with the file name and line number.

### 6.2 Case metrics

`compute_metrics(case, run)` derives these values from the `RunResult`.

| Metric | Definition |
| --- | --- |
| completed | Run status is success. |
| tool_selection_correct | The set of tools actually executed equals the set in `expected_tools`. Blocked calls are not counted as executed. |
| schema_checked | Number of executed tool calls. |
| schema_aligned | Executed calls whose status is ok or tool_error. A tool_error is a failure of the tool itself and not of the arguments. |
| tool_call_count | All recorded tool calls, blocked ones included. |
| within_call_limit | `tool_call_count <= max_tool_calls`. |
| loop_free | Status is neither loop_detected nor step_limit. |
| unhandled_error | Status is error. |
| within_budget | Status is not budget_exceeded. |
| answer_match | Every expected substring occurs in the output. True when none are listed. |
| trajectory_efficiency | If there were no calls: 1 when none were expected, else 0. Otherwise `min(1, expected_count / actual_count)`. |
| passed | completed, tool_selection_correct, within_call_limit and answer_match all true. |

### 6.3 Aggregate metrics

| Metric | Definition |
| --- | --- |
| pass_rate | Share of cases with `passed`. |
| tool_selection_accuracy | Share of cases with `tool_selection_correct`. |
| schema_alignment | Total `schema_aligned` divided by total `schema_checked` over all cases. 1 when no tool was called. |
| unhandled_error_rate | Share of cases with `unhandled_error`. |
| loop_rate | Share of cases that are not `loop_free`. |
| budget_breach_rate | Share of cases that are not `within_budget`. |
| call_limit_compliance | Share of cases with `within_call_limit`. |
| answer_match_rate | Share of cases with `answer_match`. |
| mean_trajectory_efficiency | Mean of `trajectory_efficiency`. |
| latency_p95_s | 95th percentile of run latency by the nearest-rank method. |
| mean_cost_usd | Mean cost per case. |
| judge_efficiency, judge_loop_rate | Present only when a judge produced verdicts. |

### 6.4 Thresholds and verdict

A threshold names a metric, a comparator (`min` or `max`), a value, a severity (`block` or `warn`) and the action to take on failure. The defaults are:

| Metric | Target | Severity |
| --- | --- | --- |
| tool_selection_accuracy | at least 0.92 | block |
| schema_alignment | at least 0.92 | block |
| unhandled_error_rate | at most 0 | block |
| loop_rate | at most 0 | block |
| budget_breach_rate | at most 0 | block |
| call_limit_compliance | at least 1 | block |
| answer_match_rate | at least 0.9 | block |
| latency_p95_s | at most 12.0 | warn |
| mean_trajectory_efficiency | at least 0.7 | warn |
| mean_cost_usd | at most 0.25 | warn |

The verdict is `fail` when any blocking check fails, `pass_with_warnings` when only warning checks fail, and `pass` otherwise. A threshold that refers to a metric that was not computed fails. Replace the defaults with a JSON file (`--thresholds`) containing a list of threshold objects, as in `examples/thresholds.json`.

The scorecard is written as `scorecard.json` and as a Markdown table in `scorecard.md`, which also lists failing cases with the checks that failed. `results.jsonl` holds one line per case with the full run, including all spans.

### 6.5 Trajectory judge

`TrajectoryJudge` asks a model to review a run. It is optional and independent of the deterministic metrics.

The payload sent to the model contains the task, the run status, an ordered list of LLM and tool steps (with latency, tool arguments and tool result status, each clipped) and the final answer. The system prompt asks for one JSON object with `loop_detected`, `trajectory_efficiency_score` between 0 and 1, `goal_drift` and `rationale`. The reply may be wrapped in code fences. It is validated with Pydantic, and an invalid reply raises `JudgeError`.

In `EvaluationRunner`, a judge failure is stored in `judge_error` for that case and does not stop the run or change the verdict. The judge takes a plain function `(system_prompt, user_message) -> text`, so any provider can be used. `openai_completion(client, model)` builds one for an OpenAI client, using JSON mode and temperature 0. On the command line, `--judge` uses it with the `openai` extra and `OPENAI_API_KEY`; `--judge-model` selects the model.

### 6.6 Ragas

`tracegate.evaluation.rag` evaluates retrieval-backed answers with Ragas. `evaluate_rag(samples, llm, embeddings, metrics)` takes `RagSample` objects (question, contexts, answer, optional reference) and returns the mean score per metric, skipping missing or NaN scores. The named metrics are `faithfulness`, `response_relevancy`, `context_precision` and `context_recall`. The last two need a reference answer for every sample. Ragas metric objects can be passed instead of names.

`openai_ragas_models()` returns a Ragas LLM wrapper and embeddings wrapper for OpenAI through `langchain-openai`. The `ragas` extra pins `ragas>=0.3,<0.5` together with a compatible `langchain-community` and `langchain-core`, because Ragas changes its API between minor releases and breaks against newer LangChain majors. Ragas 0.4 prints deprecation warnings for `evaluate()`, which the project's pytest configuration filters.

### 6.7 Capturing production edge cases

`CaptureSink` is a run sink. Attached to a harness, it inspects each finished run and appends a line to a JSON Lines file when any of these hold.

| Reason | Condition |
| --- | --- |
| status:<status> | The run did not succeed. |
| argument_errors | At least one tool call had invalid arguments. |
| budget_pressure | Cost reached `capture_budget_ratio` of the budget (default 0.8). |
| high_step_count | Tool calls reached `capture_step_ratio` of `max_steps` (default 0.8). |

Each record holds the input, the reasons, the status, the tools observed, the cost, the trace id, a timestamp and `expected_tools: null`. Inputs already in the file are not written twice, including after a restart.

The loop is closed by hand. Review the candidates file, set `expected_tools` (and optionally `expected_substrings`) on the entries worth keeping, then run

```
tracegate promote --candidates candidates.jsonl --dataset datasets/golden.jsonl
```

Entries with `expected_tools` set become golden cases with id `captured-<first 8 characters of the trace id>`, tag `captured` and `max_tool_calls` of the expected tool count plus 2, at least 3. Entries still `null` stay in the candidates file. Entries whose input is already in the dataset are dropped.

## 7. Installation and configuration

Requires Python 3.10 or newer.

```
pip install -e ".[dev,otlp]"
```

| Extra | Adds |
| --- | --- |
| otlp | OTLP/HTTP span exporter, needed when `TRACEGATE_OTLP_ENDPOINT` is set |
| openai | OpenAI client for `OpenAIToolAgent`, the judge and `openai_invoke` |
| anthropic | Anthropic client for `anthropic_invoke` |
| ragas | Ragas and compatible LangChain packages |
| dev | pytest, pytest-cov, ruff |

### Settings

Settings are read from environment variables with the prefix `TRACEGATE_`, from a `.env` file in the working directory, or from keyword arguments to `Settings`.

| Variable | Default | Meaning |
| --- | --- | --- |
| TRACEGATE_SERVICE_NAME | tracegate | `service.name` resource attribute |
| TRACEGATE_PROJECT_NAME | tracegate | `openinference.project.name` resource attribute |
| TRACEGATE_OTLP_ENDPOINT | unset | Full OTLP/HTTP traces URL. Empty means no export. |
| TRACEGATE_OTLP_HEADERS | empty | Comma separated `key=value` headers for the exporter |
| TRACEGATE_CONSOLE_EXPORT | false | Print spans to standard error |
| TRACEGATE_BUDGET_USD | 2.0 | Cost limit per run |
| TRACEGATE_MAX_STEPS | 25 | Tool calls allowed per run |
| TRACEGATE_LOOP_REPEATS | 3 | Repetitions that make a cycle (minimum 2) |
| TRACEGATE_LOOP_MAX_PERIOD | 3 | Longest cycle length checked |
| TRACEGATE_MAX_PAYLOAD_CHARS | 8000 | Clip length for input and output attributes |
| TRACEGATE_CAPTURE_BUDGET_RATIO | 0.8 | Budget share that flags a run |
| TRACEGATE_CAPTURE_STEP_RATIO | 0.8 | Step share that flags a run |
| TRACEGATE_PRICING_FILE | unset | JSON file of model prices |

Spans are written to standard error, not standard output, when console export is on, so that command output stays machine readable.

### Sending traces to a backend

Arize Phoenix. `docker-compose.yml` starts Phoenix with a PostgreSQL database. Phoenix accepts OTLP/HTTP on port 6006.

```
docker compose up -d
export TRACEGATE_OTLP_ENDPOINT=http://localhost:6006/v1/traces
export TRACEGATE_PROJECT_NAME=my-agent
```

Langfuse. Langfuse accepts OTLP/HTTP with basic authentication using the project's public and secret keys.

```
AUTH=$(printf '%s' 'PUBLIC_KEY:SECRET_KEY' | base64)
export TRACEGATE_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel/v1/traces
export TRACEGATE_OTLP_HEADERS="Authorization=Basic ${AUTH}"
```

Use `https://us.cloud.langfuse.com` for the US region, or the host of a self-hosted instance. Any other OTLP/HTTP collector works the same way.

Spans are exported by a batch processor. A short-lived process must flush before exit. `with AgentHarness(...) as harness:` does this on exit, and `harness.flush()` and `harness.close()` are available.

## 8. Usage

### 8.1 Command line

```
tracegate eval  [--dataset PATH] [--out DIR] [--thresholds PATH] [--agent MODULE:FACTORY]
                 [--concurrency N] [--capture PATH] [--judge] [--judge-model NAME]
tracegate run   PROMPT [--agent MODULE:FACTORY] [--spans]
tracegate promote --candidates PATH [--dataset PATH]
```

`python -m tracegate` is equivalent to the `tracegate` command.

Exit codes: 0 for pass or pass with warnings, 1 for a failed scorecard (or an unsuccessful `run`), 2 for a configuration error such as a missing dataset or a bad `--agent` value. Errors are printed to standard error.

`--agent` takes `package.module:factory`, where the factory is a callable with no arguments that returns an agent. The factory is called once per case, so every case gets its own agent state. Without `--agent`, the bundled reference agent is used.

### 8.2 Tracing a single run from Python

```python
from tracegate import AgentHarness, Settings
from tracegate.reference import ReferenceAgent

with AgentHarness(Settings(budget_usd=0.5, max_steps=10)) as harness:
    result = harness.run(ReferenceAgent(), "Query Q2 revenue and email the result to ops@example.com")

print(result.status, result.output, result.cost_usd)
for call in result.tool_calls:
    print(call.name, call.status, call.latency_s)
```

### 8.3 Writing an agent

An agent needs a `tools` registry and a `run(ctx, user_input)` method. `FunctionAgent` adapts a plain function.

- `ctx.llm(model, messages, invoke)` runs `invoke()` inside an LLM span. `invoke` returns an `LLMResponse`. The model name must have a price.
- `ctx.tool(name, arguments)` validates and executes a tool and returns a `ToolResult`. Pass whatever the model produced. Invalid arguments come back as an `argument_error` result, not as an exception.
- Raising from `run` is allowed. The harness records it as status `error`.

`examples/custom_agent.py` shows a complete agent with its own tool and schema.

### 8.4 OpenAI function calling

`OpenAIToolAgent(tools, client, model)` implements the standard tool-calling loop with the Chat Completions API. Each model call goes through `ctx.llm`, so usage and cost come from the response's `usage` field, and each requested tool call goes through `ctx.tool`. Arguments that are not valid JSON are passed on as raw text and come back as an `argument_error`. See `examples/openai_agent.py`.

### 8.5 Other providers

`openai_invoke(client, model, messages, **kwargs)` and `anthropic_invoke(client, model, messages, max_tokens=1024, system=None, **kwargs)` return the `invoke` callable for `ctx.llm`. Both read token usage from the provider response and attach the raw response to `LLMResponse.raw`. A response without usage data raises an error, because a missing count would understate cost. For another provider, build an `LLMResponse` yourself.

### 8.6 Pricing and thresholds files

Pricing, as in `examples/prices.json`:

```
{"my-fine-tune": {"input_per_million": 4.0, "output_per_million": 12.0}}
```

```
TRACEGATE_PRICING_FILE=examples/prices.json tracegate eval
```

Thresholds are a list of threshold objects, as in `examples/thresholds.json`:

```
tracegate eval --thresholds examples/thresholds.json
```

### 8.7 Evaluating from Python

```python
from tracegate import AgentHarness, Settings
from tracegate.evaluation import EvaluationRunner, load_dataset
from tracegate.reference import ReferenceAgent

cases = load_dataset("datasets/golden.jsonl")
with AgentHarness(Settings()) as harness:
    report = EvaluationRunner(harness, ReferenceAgent, concurrency=4).run(cases)

print(report.scorecard.verdict)
report.write("reports")
```

`EvaluationRunner` accepts `judge=TrajectoryJudge(...)` and `thresholds=[...]`.

### 8.8 Capturing runs in production

```python
from tracegate import AgentHarness, CaptureSink, Settings

settings = Settings()
harness = AgentHarness(settings, sink=CaptureSink("candidates.jsonl", settings))
```

### 8.9 Continuous integration

`.github/workflows/ci.yml` has two jobs. `verify` installs the package on Python 3.10, 3.11 and 3.12, runs ruff and pytest. `release-gate` runs `tracegate eval`, appends the scorecard to the job summary and uploads the reports directory as an artifact. The job fails when the scorecard verdict is `fail`.

To gate your own agent, point `--agent` at your factory and `--dataset` at your golden file, and keep the command in the pipeline that precedes deployment.

### 8.10 Docker

```
docker build -t tracegate .
docker run --rm tracegate
```

The image runs the evaluation of the bundled dataset by default. Override the command to run anything else.

## 9. Repository layout

```
tracegate/
  pyproject.toml
  README.md
  Makefile
  Dockerfile
  docker-compose.yml
  .env.example
  .gitignore
  .github/workflows/ci.yml
  datasets/golden.jsonl
  docs/DOCUMENTATION.md
  examples/
    quickstart.py
    custom_agent.py
    openai_agent.py
    rag_eval.py
    thresholds.json
    prices.json
  src/tracegate/
    __init__.py
    __main__.py
    cli.py
    config.py
    semconv.py
    models.py
    pricing.py
    budget.py
    guardrails.py
    tools.py
    telemetry.py
    harness.py
    capture.py
    integrations.py
    openai_agent.py
    reference.py
    py.typed
    evaluation/
      __init__.py
      dataset.py
      judge.py
      metrics.py
      scorecard.py
      runner.py
      rag.py
  tests/
    __init__.py
    conftest.py
    policies.py
    test_pricing_budget.py
    test_guardrails.py
    test_tools.py
    test_harness.py
    test_evaluation.py
    test_judge_capture_cli.py
    test_rag.py
```

The reference agent in `reference.py` is a deterministic stand-in for a model. A keyword-based policy decides which tool to call and reports estimated token counts (four characters per token). It exists so that the harness, the evaluation pipeline and the tests can run without a network. Real agents replace it.

## 10. Source code

### 10.1 Package and configuration

#### `pyproject.toml`

````toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "tracegate"
version = "1.0.0"
description = "Observability and evaluation harness for tool-using agents"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "opentelemetry-api>=1.25",
    "opentelemetry-sdk>=1.25",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
]

[project.optional-dependencies]
otlp = ["opentelemetry-exporter-otlp-proto-http>=1.25"]
openai = ["openai>=1.40"]
anthropic = ["anthropic>=0.34"]
ragas = [
    "ragas>=0.3,<0.5",
    "langchain-openai>=0.2,<1",
    "langchain-community>=0.3,<0.4",
    "langchain-core>=0.3,<1",
]
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.5"]

[project.scripts]
tracegate = "tracegate.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
tracegate = ["py.typed"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
filterwarnings = ["ignore:.*ragas.*:DeprecationWarning"]

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
````

#### `src/tracegate/__init__.py`

````python
from tracegate.budget import BudgetExceeded, CostTracker
from tracegate.capture import CaptureSink
from tracegate.config import Settings
from tracegate.guardrails import LoopDetected, LoopGuard, StepLimitExceeded
from tracegate.harness import Agent, AgentHarness, FunctionAgent, RunContext
from tracegate.models import LLMResponse, RunResult, SpanRecord, ToolCall
from tracegate.pricing import ModelPrice, PriceBook, UnknownModelError
from tracegate.tools import ToolRegistry, ToolResult, ToolSpec

__version__ = "1.0.0"

__all__ = [
    "Agent",
    "AgentHarness",
    "BudgetExceeded",
    "CaptureSink",
    "CostTracker",
    "FunctionAgent",
    "LLMResponse",
    "LoopDetected",
    "LoopGuard",
    "ModelPrice",
    "PriceBook",
    "RunContext",
    "RunResult",
    "Settings",
    "SpanRecord",
    "StepLimitExceeded",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "UnknownModelError",
    "__version__",
]
````

#### `src/tracegate/__main__.py`

````python
import sys

from tracegate.cli import main

sys.exit(main())
````

#### `src/tracegate/config.py`

````python
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRACEGATE_", env_file=".env", extra="ignore")

    service_name: str = "tracegate"
    project_name: str = "tracegate"
    otlp_endpoint: str | None = None
    otlp_headers: str = ""
    console_export: bool = False
    budget_usd: float = Field(default=2.0, gt=0)
    max_steps: int = Field(default=25, ge=1)
    loop_repeats: int = Field(default=3, ge=2)
    loop_max_period: int = Field(default=3, ge=1)
    max_payload_chars: int = Field(default=8000, ge=64)
    capture_budget_ratio: float = Field(default=0.8, gt=0, le=1)
    capture_step_ratio: float = Field(default=0.8, gt=0, le=1)
    pricing_file: str | None = None

    @field_validator("otlp_endpoint", "pricing_file", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def otlp_header_map(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for pair in self.otlp_headers.split(","):
            pair = pair.strip()
            if not pair:
                continue
            key, separator, value = pair.partition("=")
            if not separator or not key.strip():
                raise ValueError(f"invalid OTLP header entry: {pair!r}")
            headers[key.strip()] = value.strip()
        return headers
````

#### `src/tracegate/semconv.py`

````python
SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
INPUT_MIME = "input.mime_type"
OUTPUT_VALUE = "output.value"
OUTPUT_MIME = "output.mime_type"
SESSION_ID = "session.id"
LLM_MODEL_NAME = "llm.model_name"
LLM_TOKENS_PROMPT = "llm.token_count.prompt"
LLM_TOKENS_COMPLETION = "llm.token_count.completion"
LLM_TOKENS_TOTAL = "llm.token_count.total"
TOOL_NAME = "tool.name"
TOOL_PARAMETERS = "tool.parameters"
LLM_COST_USD = "agent.llm.cost_usd"
TOOL_STATUS = "agent.tool.status"
TOOL_RETRYABLE = "agent.tool.retryable"
RUN_STATUS = "agent.run.status"
RUN_COST_USD = "agent.run.cost_usd"
RUN_LLM_CALLS = "agent.run.llm_calls"
RUN_TOOL_CALLS = "agent.run.tool_calls"
KIND_AGENT = "AGENT"
KIND_LLM = "LLM"
KIND_TOOL = "TOOL"
````

#### `src/tracegate/models.py`

````python
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from tracegate.semconv import KIND_TOOL, TOOL_NAME, TOOL_PARAMETERS, TOOL_STATUS

RunStatus = Literal["success", "budget_exceeded", "loop_detected", "step_limit", "error"]


class LLMResponse(BaseModel):
    text: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    raw: Any = Field(default=None, exclude=True, repr=False)


class SpanRecord(BaseModel):
    trace_id: str
    span_id: str
    parent_id: str | None = None
    name: str
    kind: str
    status: str
    start_ns: int
    end_ns: int
    attributes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @property
    def latency_s(self) -> float:
        return (self.end_ns - self.start_ns) / 1e9


class ToolCall(BaseModel):
    name: str
    arguments: Any = None
    status: str
    latency_s: float


class RunResult(BaseModel):
    trace_id: str
    session_id: str | None = None
    input: str
    status: RunStatus
    output: str | None = None
    error: str | None = None
    latency_s: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    llm_calls: int
    tool_calls: list[ToolCall] = Field(default_factory=list)
    spans: list[SpanRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def extract_tool_calls(spans: list[SpanRecord]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for span in spans:
        if span.kind != KIND_TOOL:
            continue
        raw = span.attributes.get(TOOL_PARAMETERS)
        arguments: Any = raw
        if isinstance(raw, str):
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = raw
        calls.append(
            ToolCall(
                name=str(span.attributes.get(TOOL_NAME, span.name)),
                arguments=arguments,
                status=str(span.attributes.get(TOOL_STATUS, "error")),
                latency_s=span.latency_s,
            )
        )
    return calls
````

#### `src/tracegate/pricing.py`

````python
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from tracegate.config import Settings


class ModelPrice(BaseModel):
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        total = input_tokens * self.input_per_million + output_tokens * self.output_per_million
        return total / 1_000_000


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "gpt-4o": ModelPrice(input_per_million=2.5, output_per_million=10.0),
    "gpt-4o-mini": ModelPrice(input_per_million=0.15, output_per_million=0.6),
    "claude-3-5-sonnet": ModelPrice(input_per_million=3.0, output_per_million=15.0),
    "claude-3-5-haiku": ModelPrice(input_per_million=0.8, output_per_million=4.0),
    "mock-llm": ModelPrice(input_per_million=1.0, output_per_million=2.0),
}


class UnknownModelError(Exception):
    def __init__(self, model: str):
        super().__init__(f"no price configured for model {model!r}")
        self.model = model


class PriceBook:
    def __init__(self, prices: dict[str, ModelPrice] | None = None):
        self._prices = dict(DEFAULT_PRICES if prices is None else prices)

    @classmethod
    def from_file(cls, path: str | Path) -> PriceBook:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        prices = dict(DEFAULT_PRICES)
        prices.update({name: ModelPrice.model_validate(entry) for name, entry in data.items()})
        return cls(prices)

    @classmethod
    def from_settings(cls, settings: Settings) -> PriceBook:
        if settings.pricing_file:
            return cls.from_file(settings.pricing_file)
        return cls()

    def price_for(self, model: str) -> ModelPrice:
        if model in self._prices:
            return self._prices[model]
        matches = [name for name in self._prices if model.startswith(name)]
        if not matches:
            raise UnknownModelError(model)
        return self._prices[max(matches, key=len)]

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return self.price_for(model).cost(input_tokens, output_tokens)
````

#### `src/tracegate/budget.py`

````python
from __future__ import annotations

import threading

from pydantic import BaseModel

from tracegate.pricing import PriceBook


class BudgetExceeded(Exception):
    def __init__(self, spent: float, limit: float):
        super().__init__(f"budget exceeded: spent ${spent:.6f} of ${limit:.6f}")
        self.spent = spent
        self.limit = limit


class UsageRecord(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CostTracker:
    def __init__(self, limit_usd: float, prices: PriceBook):
        self.limit_usd = limit_usd
        self._prices = prices
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()

    def record(self, model: str, input_tokens: int, output_tokens: int) -> UsageRecord:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        usage = UsageRecord(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self._prices.cost(model, input_tokens, output_tokens),
        )
        with self._lock:
            self._records.append(usage)
        return usage

    def check(self) -> None:
        spent = self.total_cost
        if spent > self.limit_usd:
            raise BudgetExceeded(spent, self.limit_usd)

    @property
    def records(self) -> list[UsageRecord]:
        with self._lock:
            return list(self._records)

    @property
    def total_cost(self) -> float:
        return sum(record.cost_usd for record in self.records)

    @property
    def input_tokens(self) -> int:
        return sum(record.input_tokens for record in self.records)

    @property
    def output_tokens(self) -> int:
        return sum(record.output_tokens for record in self.records)

    def breakdown(self) -> dict[str, dict[str, float]]:
        summary: dict[str, dict[str, float]] = {}
        for record in self.records:
            entry = summary.setdefault(
                record.model,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            )
            entry["calls"] += 1
            entry["input_tokens"] += record.input_tokens
            entry["output_tokens"] += record.output_tokens
            entry["cost_usd"] += record.cost_usd
        return summary
````

#### `src/tracegate/guardrails.py`

````python
from __future__ import annotations

import json
from typing import Any


class LoopDetected(Exception):
    def __init__(self, tool: str, period: int, repeats: int):
        super().__init__(
            f"repeating pattern detected at tool {tool!r}: cycle of {period} call(s) "
            f"repeated {repeats} times"
        )
        self.tool = tool
        self.period = period
        self.repeats = repeats


class StepLimitExceeded(Exception):
    def __init__(self, limit: int):
        super().__init__(f"tool call limit of {limit} reached")
        self.limit = limit


class LoopGuard:
    def __init__(self, max_steps: int, repeats: int = 3, max_period: int = 3):
        if max_steps < 1 or repeats < 2 or max_period < 1:
            raise ValueError("max_steps >= 1, repeats >= 2 and max_period >= 1 are required")
        self.max_steps = max_steps
        self.repeats = repeats
        self.max_period = max_period
        self._history: list[tuple[str, str]] = []

    @property
    def steps(self) -> int:
        return len(self._history)

    def observe(self, tool: str, arguments: Any) -> None:
        if len(self._history) >= self.max_steps:
            raise StepLimitExceeded(self.max_steps)
        fingerprint = json.dumps(arguments, sort_keys=True, default=str)
        self._history.append((tool, fingerprint))
        period = self._cycle_period()
        if period:
            raise LoopDetected(tool, period, self.repeats)

    def _cycle_period(self) -> int | None:
        size = len(self._history)
        for period in range(1, self.max_period + 1):
            window_size = period * self.repeats
            if size < window_size:
                continue
            window = self._history[-window_size:]
            unit = window[:period]
            if all(window[index] == unit[index % period] for index in range(window_size)):
                return period
        return None
````

#### `src/tracegate/tools.py`

````python
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

ToolStatus = Literal["ok", "argument_error", "unknown_tool", "tool_error"]


class ToolResult(BaseModel):
    status: ToolStatus
    output: Any = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any], Any]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def register(
        self, name: str, args_model: type[BaseModel], description: str = ""
    ) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        def decorator(handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
            self.add(
                ToolSpec(
                    name=name,
                    description=description,
                    args_model=args_model,
                    handler=handler,
                )
            )
            return handler

        return decorator

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._tools.values()]

    def execute(self, name: str, raw_arguments: Any) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                status="unknown_tool",
                errors=[{"type": "unknown_tool", "msg": f"no tool named {name!r}"}],
                retryable=True,
            )
        try:
            validated = spec.args_model.model_validate(raw_arguments)
        except ValidationError as exc:
            details = json.loads(
                exc.json(include_url=False, include_context=False, include_input=False)
            )
            return ToolResult(status="argument_error", errors=details, retryable=True)
        try:
            output = spec.handler(validated)
        except Exception as exc:
            return ToolResult(
                status="tool_error",
                errors=[{"type": type(exc).__name__, "msg": str(exc)}],
                retryable=False,
            )
        return ToolResult(status="ok", output=_jsonable(output))
````

#### `src/tracegate/telemetry.py`

````python
from __future__ import annotations

import sys
import threading
from collections import defaultdict

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import StatusCode

from tracegate.config import Settings
from tracegate.models import SpanRecord
from tracegate.semconv import SPAN_KIND


class TraceCollector(SpanProcessor):
    def __init__(self) -> None:
        self._spans: dict[int, list[ReadableSpan]] = defaultdict(list)
        self._lock = threading.Lock()

    def on_start(self, span, parent_context=None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        with self._lock:
            self._spans[span.get_span_context().trace_id].append(span)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def take(self, trace_id: int) -> list[ReadableSpan]:
        with self._lock:
            spans = self._spans.pop(trace_id, [])
        return sorted(spans, key=lambda span: span.start_time or 0)


def to_record(span: ReadableSpan) -> SpanRecord:
    context = span.get_span_context()
    attributes = dict(span.attributes or {})
    status = span.status
    return SpanRecord(
        trace_id=format(context.trace_id, "032x"),
        span_id=format(context.span_id, "016x"),
        parent_id=format(span.parent.span_id, "016x") if span.parent else None,
        name=span.name,
        kind=str(attributes.get(SPAN_KIND, "UNKNOWN")),
        status=status.status_code.name,
        start_ns=span.start_time or 0,
        end_ns=span.end_time or 0,
        attributes=attributes,
        error=status.description if status.status_code is StatusCode.ERROR else None,
    )


class Telemetry:
    def __init__(self, settings: Settings):
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "openinference.project.name": settings.project_name,
            }
        )
        self.provider = TracerProvider(resource=resource)
        self.collector = TraceCollector()
        self.provider.add_span_processor(self.collector)
        if settings.otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "OTLP export requires the extra: pip install 'tracegate[otlp]'"
                ) from exc
            exporter = OTLPSpanExporter(
                endpoint=settings.otlp_endpoint,
                headers=settings.otlp_header_map or None,
            )
            self.provider.add_span_processor(BatchSpanProcessor(exporter))
        if settings.console_export:
            self.provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr))
            )
        self.tracer = self.provider.get_tracer("tracegate")

    def flush(self, timeout_ms: int = 10000) -> bool:
        return self.provider.force_flush(timeout_ms)

    def shutdown(self) -> None:
        self.provider.shutdown()
````

#### `src/tracegate/harness.py`

````python
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from opentelemetry.trace import Status, StatusCode, Tracer

from tracegate.budget import BudgetExceeded, CostTracker
from tracegate.config import Settings
from tracegate.guardrails import LoopDetected, LoopGuard, StepLimitExceeded
from tracegate.models import LLMResponse, RunResult, RunStatus, extract_tool_calls
from tracegate.pricing import PriceBook
from tracegate.semconv import (
    INPUT_MIME,
    INPUT_VALUE,
    KIND_AGENT,
    KIND_LLM,
    KIND_TOOL,
    LLM_COST_USD,
    LLM_MODEL_NAME,
    LLM_TOKENS_COMPLETION,
    LLM_TOKENS_PROMPT,
    LLM_TOKENS_TOTAL,
    OUTPUT_MIME,
    OUTPUT_VALUE,
    RUN_COST_USD,
    RUN_LLM_CALLS,
    RUN_STATUS,
    RUN_TOOL_CALLS,
    SESSION_ID,
    SPAN_KIND,
    TOOL_NAME,
    TOOL_PARAMETERS,
    TOOL_RETRYABLE,
    TOOL_STATUS,
)
from tracegate.telemetry import Telemetry, to_record
from tracegate.tools import ToolRegistry, ToolResult

logger = logging.getLogger("tracegate")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _dump(value: Any, limit: int) -> str:
    return _clip(json.dumps(value, default=str, ensure_ascii=False), limit)


def _scalar(value: Any) -> str | bool | int | float:
    if isinstance(value, str | bool | int | float):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _classify(error: Exception) -> RunStatus:
    if isinstance(error, BudgetExceeded):
        return "budget_exceeded"
    if isinstance(error, LoopDetected):
        return "loop_detected"
    if isinstance(error, StepLimitExceeded):
        return "step_limit"
    return "error"


class RunContext:
    def __init__(
        self,
        tracer: Tracer,
        tools: ToolRegistry,
        cost: CostTracker,
        guard: LoopGuard,
        payload_limit: int,
        session_id: str | None = None,
    ):
        self.tracer = tracer
        self.tools = tools
        self.cost = cost
        self.guard = guard
        self.session_id = session_id
        self._payload_limit = payload_limit

    def llm(
        self,
        model: str,
        messages: list[dict[str, Any]],
        invoke: Callable[[], LLMResponse],
    ) -> LLMResponse:
        with self.tracer.start_as_current_span("llm.call") as span:
            span.set_attribute(SPAN_KIND, KIND_LLM)
            span.set_attribute(LLM_MODEL_NAME, model)
            span.set_attribute(INPUT_VALUE, _dump(messages, self._payload_limit))
            span.set_attribute(INPUT_MIME, "application/json")
            response = invoke()
            usage = self.cost.record(model, response.input_tokens, response.output_tokens)
            span.set_attribute(OUTPUT_VALUE, _clip(response.text, self._payload_limit))
            span.set_attribute(OUTPUT_MIME, "text/plain")
            span.set_attribute(LLM_TOKENS_PROMPT, usage.input_tokens)
            span.set_attribute(LLM_TOKENS_COMPLETION, usage.output_tokens)
            span.set_attribute(LLM_TOKENS_TOTAL, usage.input_tokens + usage.output_tokens)
            span.set_attribute(LLM_COST_USD, usage.cost_usd)
            self.cost.check()
            return response

    def tool(self, name: str, arguments: Any) -> ToolResult:
        with self.tracer.start_as_current_span(f"tool.{name}") as span:
            span.set_attribute(SPAN_KIND, KIND_TOOL)
            span.set_attribute(TOOL_NAME, name)
            span.set_attribute(TOOL_PARAMETERS, _dump(arguments, self._payload_limit))
            span.set_attribute(INPUT_VALUE, _dump(arguments, self._payload_limit))
            span.set_attribute(INPUT_MIME, "application/json")
            try:
                self.guard.observe(name, arguments)
            except (LoopDetected, StepLimitExceeded):
                span.set_attribute(TOOL_STATUS, "blocked")
                raise
            result = self.tools.execute(name, arguments)
            span.set_attribute(
                OUTPUT_VALUE, _dump(result.model_dump(mode="json"), self._payload_limit)
            )
            span.set_attribute(OUTPUT_MIME, "application/json")
            span.set_attribute(TOOL_STATUS, result.status)
            span.set_attribute(TOOL_RETRYABLE, result.retryable)
            if not result.ok:
                span.set_status(Status(StatusCode.ERROR, result.status))
            return result


class Agent(Protocol):
    tools: ToolRegistry

    def run(self, ctx: RunContext, user_input: str) -> str: ...


class FunctionAgent:
    def __init__(self, tools: ToolRegistry, fn: Callable[[RunContext, str], str]):
        self.tools = tools
        self._fn = fn

    def run(self, ctx: RunContext, user_input: str) -> str:
        return self._fn(ctx, user_input)


class RunSink(Protocol):
    def consider(self, result: RunResult) -> Any: ...


class AgentHarness:
    def __init__(
        self,
        settings: Settings | None = None,
        prices: PriceBook | None = None,
        telemetry: Telemetry | None = None,
        sink: RunSink | None = None,
    ):
        self.settings = settings or Settings()
        self.prices = prices or PriceBook.from_settings(self.settings)
        self.telemetry = telemetry or Telemetry(self.settings)
        self.sink = sink

    def __enter__(self) -> AgentHarness:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def flush(self) -> bool:
        return self.telemetry.flush()

    def close(self) -> None:
        self.telemetry.flush()
        self.telemetry.shutdown()

    def run(
        self,
        agent: Agent,
        user_input: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult:
        settings = self.settings
        limit = settings.max_payload_chars
        cost = CostTracker(settings.budget_usd, self.prices)
        guard = LoopGuard(settings.max_steps, settings.loop_repeats, settings.loop_max_period)
        context = RunContext(
            self.telemetry.tracer, agent.tools, cost, guard, limit, session_id=session_id
        )
        status: RunStatus = "success"
        output: str | None = None
        error: str | None = None
        started = time.perf_counter()
        with self.telemetry.tracer.start_as_current_span("agent.run") as span:
            trace_id = span.get_span_context().trace_id
            span.set_attribute(SPAN_KIND, KIND_AGENT)
            span.set_attribute(INPUT_VALUE, _clip(user_input, limit))
            if session_id:
                span.set_attribute(SESSION_ID, session_id)
            for key, value in (metadata or {}).items():
                span.set_attribute(f"metadata.{key}", _scalar(value))
            try:
                output = _text(agent.run(context, user_input))
            except Exception as exc:
                status = _classify(exc)
                error = f"{type(exc).__name__}: {exc}"
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, error))
            records = cost.records
            span.set_attribute(RUN_STATUS, status)
            span.set_attribute(RUN_COST_USD, cost.total_cost)
            span.set_attribute(RUN_LLM_CALLS, len(records))
            span.set_attribute(LLM_TOKENS_PROMPT, cost.input_tokens)
            span.set_attribute(LLM_TOKENS_COMPLETION, cost.output_tokens)
            span.set_attribute(LLM_TOKENS_TOTAL, cost.input_tokens + cost.output_tokens)
            span.set_attribute(RUN_TOOL_CALLS, guard.steps)
            if output is not None:
                span.set_attribute(OUTPUT_VALUE, _clip(output, limit))
        latency = time.perf_counter() - started
        spans = [to_record(raw) for raw in self.telemetry.collector.take(trace_id)]
        result = RunResult(
            trace_id=format(trace_id, "032x"),
            session_id=session_id,
            input=user_input,
            status=status,
            output=output,
            error=error,
            latency_s=latency,
            cost_usd=cost.total_cost,
            input_tokens=cost.input_tokens,
            output_tokens=cost.output_tokens,
            llm_calls=len(records),
            tool_calls=extract_tool_calls(spans),
            spans=spans,
            metadata=dict(metadata or {}),
        )
        if self.sink is not None:
            try:
                self.sink.consider(result)
            except Exception:
                logger.exception("run sink failed for trace %s", result.trace_id)
        return result
````

#### `src/tracegate/capture.py`

````python
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tracegate.config import Settings
from tracegate.models import RunResult


class CaptureSink:
    def __init__(self, path: str | Path, settings: Settings):
        self.path = Path(path)
        self.settings = settings
        self._lock = threading.Lock()
        self._seen = {record["input"] for record in read_candidates(self.path)}

    def reasons(self, result: RunResult) -> list[str]:
        found: list[str] = []
        if result.status != "success":
            found.append(f"status:{result.status}")
        if any(call.status == "argument_error" for call in result.tool_calls):
            found.append("argument_errors")
        if result.cost_usd >= self.settings.capture_budget_ratio * self.settings.budget_usd:
            found.append("budget_pressure")
        if len(result.tool_calls) >= self.settings.capture_step_ratio * self.settings.max_steps:
            found.append("high_step_count")
        return found

    def consider(self, result: RunResult) -> list[str]:
        found = self.reasons(result)
        if not found:
            return found
        record: dict[str, Any] = {
            "input": result.input,
            "reasons": found,
            "status": result.status,
            "observed_tools": [call.name for call in result.tool_calls],
            "cost_usd": result.cost_usd,
            "trace_id": result.trace_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "expected_tools": None,
        }
        with self._lock:
            if result.input in self._seen:
                return found
            self._seen.add(result.input)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return found


def read_candidates(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
````

#### `src/tracegate/integrations.py`

````python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tracegate.models import LLMResponse


def openai_invoke(
    client: Any, model: str, messages: list[dict[str, Any]], **kwargs: Any
) -> Callable[[], LLMResponse]:
    def invoke() -> LLMResponse:
        response = client.chat.completions.create(model=model, messages=messages, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is None:
            raise ValueError("the provider response did not include token usage")
        return LLMResponse(
            text=response.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            raw=response,
        )

    return invoke


def anthropic_invoke(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 1024,
    system: str | None = None,
    **kwargs: Any,
) -> Callable[[], LLMResponse]:
    def invoke() -> LLMResponse:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            **kwargs,
        }
        if system is not None:
            params["system"] = system
        response = client.messages.create(**params)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return LLMResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            raw=response,
        )

    return invoke


def openai_completion(client: Any, model: str) -> Callable[[str, str], str]:
    def complete(system: str, user: str) -> str:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""

    return complete
````

#### `src/tracegate/openai_agent.py`

````python
from __future__ import annotations

import json
from typing import Any

from tracegate.harness import RunContext
from tracegate.integrations import openai_invoke
from tracegate.tools import ToolRegistry


class OpenAIToolAgent:
    def __init__(
        self,
        tools: ToolRegistry,
        client: Any,
        model: str,
        system_prompt: str = "You are a precise assistant. Use the provided tools when needed.",
        temperature: float = 0.0,
    ):
        self.tools = tools
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature

    def run(self, ctx: RunContext, user_input: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        definitions = [{"type": "function", "function": schema} for schema in self.tools.schemas()]
        extra: dict[str, Any] = {"temperature": self.temperature}
        if definitions:
            extra["tools"] = definitions
        while True:
            response = ctx.llm(
                self.model,
                messages,
                openai_invoke(self.client, self.model, messages, **extra),
            )
            message = response.raw.choices[0].message
            calls = getattr(message, "tool_calls", None)
            if not calls:
                return message.content or ""
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in calls
                    ],
                }
            )
            for call in calls:
                try:
                    arguments: Any = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = call.function.arguments
                result = ctx.tool(call.function.name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.model_dump_json(),
                    }
                )
````

#### `src/tracegate/reference.py`

````python
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tracegate.harness import RunContext
from tracegate.models import LLMResponse
from tracegate.tools import ToolRegistry

Policy = Callable[[list[dict[str, Any]]], dict[str, Any]]

REVENUE_ROWS = [
    ("north", "Q1", 100000),
    ("north", "Q2", 120000),
    ("north", "Q3", 150000),
    ("south", "Q1", 80000),
    ("south", "Q2", 90000),
    ("south", "Q3", 110000),
    ("east", "Q1", 70000),
    ("east", "Q2", 85000),
    ("east", "Q3", 95000),
    ("west", "Q1", 50000),
    ("west", "Q2", 52000),
    ("west", "Q3", 57000),
]

DOCUMENTS = [
    {
        "id": "rollback-guide",
        "title": "Deployment rollback procedure",
        "text": "Revert to the previous release tag, redeploy and verify the health checks.",
    },
    {
        "id": "incident-runbook",
        "title": "Incident response runbook",
        "text": "Page the on-call engineer, open an incident channel and record a timeline.",
    },
    {
        "id": "onboarding-checklist",
        "title": "New engineer onboarding",
        "text": "Request repository access, set up the local toolchain and read the architecture.",
    },
    {
        "id": "data-retention",
        "title": "Data retention policy",
        "text": "Operational logs are retained for ninety days and analytics tables for two years.",
    },
]

EMAIL_PATTERN = r"[\w.+-]+@[\w-]+\.[\w.-]+"
REVENUE_WORDS = ("revenue", "sales", "database", "query")
DOCUMENT_WORDS = ("runbook", "documentation", "docs", "how do i", "procedure")
EMAIL_WORDS = ("email", "alert", "notify")
STOPWORDS = {"the", "for", "and", "with", "from", "that", "this", "are", "how", "can"}


class DatabaseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_string: str = Field(min_length=10)
    row_limit: int = Field(default=100, ge=1, le=500)

    @field_validator("query_string")
    @classmethod
    def read_only(cls, value: str) -> str:
        statement = value.strip().rstrip(";").strip()
        if ";" in statement:
            raise ValueError("multiple statements are not allowed")
        if not statement.lower().startswith("select"):
            raise ValueError("only SELECT statements are allowed")
        return statement


class SendEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


class SearchDocuments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in STOPWORDS}


def build_database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute("CREATE TABLE revenue (region TEXT, quarter TEXT, amount INTEGER)")
    connection.executemany("INSERT INTO revenue VALUES (?, ?, ?)", REVENUE_ROWS)
    connection.commit()
    connection.execute("PRAGMA query_only = ON")
    return connection


def build_registry(connection: sqlite3.Connection, outbox: list[dict[str, Any]]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(
        "query_database",
        DatabaseQuery,
        "Run a read-only SQL SELECT against the revenue table (region, quarter, amount).",
    )
    def query_database(args: DatabaseQuery) -> dict[str, Any]:
        cursor = connection.execute(args.query_string)
        columns = [column[0] for column in cursor.description or []]
        rows = [list(row) for row in cursor.fetchmany(args.row_limit)]
        return {"columns": columns, "rows": rows}

    @registry.register(
        "send_email",
        SendEmail,
        "Queue an email message for delivery to a single recipient.",
    )
    def send_email(args: SendEmail) -> dict[str, Any]:
        message_id = f"msg-{len(outbox) + 1}"
        outbox.append({"id": message_id, **args.model_dump()})
        return {"status": "queued", "message_id": message_id, "recipient": args.recipient}

    @registry.register(
        "search_documents",
        SearchDocuments,
        "Search the internal documentation corpus and return the best matching documents.",
    )
    def search_documents(args: SearchDocuments) -> list[dict[str, Any]]:
        wanted = _tokens(args.query)
        scored = []
        for document in DOCUMENTS:
            score = len(wanted & _tokens(f"{document['title']} {document['text']}"))
            if score:
                scored.append((score, document))
        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [
            {"id": document["id"], "title": document["title"], "score": score}
            for score, document in scored[: args.top_k]
        ]

    return registry


def _final(answer: str) -> dict[str, Any]:
    return {"action": "final", "answer": answer}


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"action": "tool", "tool": tool, "arguments": arguments}


def _plan(lowered: str) -> list[str]:
    plan = []
    if any(word in lowered for word in REVENUE_WORDS):
        plan.append("query_database")
    if any(word in lowered for word in DOCUMENT_WORDS):
        plan.append("search_documents")
    if any(word in lowered for word in EMAIL_WORDS):
        plan.append("send_email")
    return plan


def _summarize(message: dict[str, Any]) -> str:
    payload = json.loads(message["content"])
    if payload["status"] == "ok":
        return f"{message['name']}: {json.dumps(payload['output'], sort_keys=True)}"
    return f"{message['name']} failed with {payload['status']}"


def _arguments(tool: str, request: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    if tool == "query_database":
        quarter = re.search(r"\bq([1-4])\b", request.lower())
        if quarter:
            query = (
                f"SELECT SUM(amount) AS total FROM revenue WHERE quarter = 'Q{quarter.group(1)}'"
            )
        else:
            query = (
                "SELECT quarter, SUM(amount) AS total FROM revenue "
                "GROUP BY quarter ORDER BY quarter"
            )
        return {"query_string": query, "row_limit": 100}
    if tool == "search_documents":
        return {"query": request, "top_k": 3}
    address = re.search(EMAIL_PATTERN, request)
    body = " ".join(_summarize(message) for message in observations) or request
    return {
        "recipient": address.group(0) if address else "admin@example.com",
        "subject": "Automated alert",
        "body": body,
    }


def keyword_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    request = messages[1]["content"]
    plan = _plan(request.lower())
    observations = [message for message in messages if message["role"] == "tool"]
    if not plan:
        return _final("I can only help with revenue queries, document search and alert emails.")
    completed = {message["name"] for message in observations}
    pending = [tool for tool in plan if tool not in completed]
    if not pending:
        return _final(" ".join(_summarize(message) for message in observations))
    return _call(pending[0], _arguments(pending[0], request, observations))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ReferenceAgent:
    def __init__(self, policy: Policy = keyword_policy, model: str = "mock-llm"):
        self.policy = policy
        self.model = model
        self.outbox: list[dict[str, Any]] = []
        self.database = build_database()
        self.tools = build_registry(self.database, self.outbox)

    def _complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        action = self.policy(messages)
        text = json.dumps(action)
        return LLMResponse(
            text=text,
            input_tokens=estimate_tokens(json.dumps(messages)),
            output_tokens=estimate_tokens(text),
        )

    def run(self, ctx: RunContext, user_input: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Plan tool calls and answer the user."},
            {"role": "user", "content": user_input},
        ]
        while True:
            response = ctx.llm(self.model, messages, lambda: self._complete(messages))
            action = json.loads(response.text)
            messages.append({"role": "assistant", "content": response.text})
            if action["action"] == "final":
                return action["answer"]
            result = ctx.tool(action["tool"], action["arguments"])
            messages.append(
                {"role": "tool", "name": action["tool"], "content": result.model_dump_json()}
            )


def create_agent() -> ReferenceAgent:
    return ReferenceAgent()
````

#### `src/tracegate/cli.py`

````python
from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence

from tracegate.capture import CaptureSink
from tracegate.config import Settings
from tracegate.evaluation import (
    EvaluationRunner,
    TrajectoryJudge,
    load_dataset,
    load_thresholds,
    promote_candidates,
)
from tracegate.harness import Agent, AgentHarness
from tracegate.integrations import openai_completion
from tracegate.reference import create_agent


def load_factory(spec: str | None) -> Callable[[], Agent]:
    if not spec:
        return create_agent
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("agent must be given as 'package.module:factory'")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise ValueError(f"{spec} is not callable")
    return factory


def _build_judge(model: str) -> TrajectoryJudge:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("the judge requires the extra: pip install 'tracegate[openai]'") from exc
    return TrajectoryJudge(openai_completion(OpenAI(), model))


def _command_eval(args: argparse.Namespace) -> int:
    settings = Settings()
    sink = CaptureSink(args.capture, settings) if args.capture else None
    thresholds = load_thresholds(args.thresholds) if args.thresholds else None
    judge = _build_judge(args.judge_model) if args.judge else None
    cases = load_dataset(args.dataset)
    with AgentHarness(settings, sink=sink) as harness:
        runner = EvaluationRunner(
            harness,
            load_factory(args.agent),
            judge=judge,
            concurrency=args.concurrency,
            thresholds=thresholds,
        )
        report = runner.run(cases)
    report.write(args.out)
    sys.stdout.write(report.to_markdown())
    return 1 if report.scorecard.verdict == "fail" else 0


def _command_run(args: argparse.Namespace) -> int:
    settings = Settings()
    with AgentHarness(settings) as harness:
        result = harness.run(load_factory(args.agent)(), args.prompt)
    exclude = None if args.spans else {"spans"}
    sys.stdout.write(result.model_dump_json(indent=2, exclude=exclude) + "\n")
    return 0 if result.status == "success" else 1


def _command_promote(args: argparse.Namespace) -> int:
    count = promote_candidates(args.candidates, args.dataset)
    sys.stdout.write(f"promoted {count} case(s) into {args.dataset}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracegate")
    commands = parser.add_subparsers(dest="command", required=True)

    evaluate = commands.add_parser("eval", help="run the golden dataset and gate on the scorecard")
    evaluate.add_argument("--dataset", default="datasets/golden.jsonl")
    evaluate.add_argument("--out", default="reports")
    evaluate.add_argument("--thresholds")
    evaluate.add_argument("--agent")
    evaluate.add_argument("--concurrency", type=int, default=4)
    evaluate.add_argument("--capture")
    evaluate.add_argument("--judge", action="store_true")
    evaluate.add_argument("--judge-model", default="gpt-4o-mini")
    evaluate.set_defaults(handler=_command_eval)

    run = commands.add_parser("run", help="run a single prompt and print the traced result")
    run.add_argument("prompt")
    run.add_argument("--agent")
    run.add_argument("--spans", action="store_true")
    run.set_defaults(handler=_command_run)

    promote = commands.add_parser("promote", help="move reviewed candidates into the dataset")
    promote.add_argument("--candidates", required=True)
    promote.add_argument("--dataset", default="datasets/golden.jsonl")
    promote.set_defaults(handler=_command_promote)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, OSError, ImportError, AttributeError, RuntimeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
````

#### `src/tracegate/evaluation/__init__.py`

````python
from tracegate.evaluation.dataset import (
    GoldenCase,
    append_cases,
    load_dataset,
    promote_candidates,
)
from tracegate.evaluation.judge import JudgeError, JudgeVerdict, TrajectoryJudge
from tracegate.evaluation.metrics import CaseMetrics, CaseResult, compute_metrics
from tracegate.evaluation.runner import EvaluationReport, EvaluationRunner
from tracegate.evaluation.scorecard import (
    DEFAULT_THRESHOLDS,
    Check,
    Scorecard,
    Threshold,
    build_scorecard,
    load_thresholds,
)

__all__ = [
    "CaseMetrics",
    "CaseResult",
    "Check",
    "DEFAULT_THRESHOLDS",
    "EvaluationReport",
    "EvaluationRunner",
    "GoldenCase",
    "JudgeError",
    "JudgeVerdict",
    "Scorecard",
    "Threshold",
    "TrajectoryJudge",
    "append_cases",
    "build_scorecard",
    "compute_metrics",
    "load_dataset",
    "load_thresholds",
    "promote_candidates",
]
````

#### `src/tracegate/evaluation/dataset.py`

````python
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from tracegate.capture import read_candidates


class GoldenCase(BaseModel):
    id: str
    input: str
    expected_tools: list[str] = Field(default_factory=list)
    max_tool_calls: int = Field(default=5, ge=0)
    expected_substrings: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def load_dataset(path: str | Path) -> list[GoldenCase]:
    source = Path(path)
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = GoldenCase.model_validate_json(line)
        except ValidationError as exc:
            raise ValueError(f"{source}:{number}: {exc}") from exc
        if case.id in seen:
            raise ValueError(f"{source}:{number}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError(f"{source}: dataset is empty")
    return cases


def append_cases(path: str | Path, cases: list[GoldenCase]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for case in cases:
            handle.write(case.model_dump_json() + "\n")


def promote_candidates(candidates_path: str | Path, dataset_path: str | Path) -> int:
    candidates = read_candidates(candidates_path)
    dataset = Path(dataset_path)
    existing = load_dataset(dataset) if dataset.exists() else []
    known_inputs = {case.input for case in existing}
    known_ids = {case.id for case in existing}
    promoted: list[GoldenCase] = []
    remaining: list[dict] = []
    for record in candidates:
        expected = record.get("expected_tools")
        if expected is None:
            remaining.append(record)
            continue
        if record["input"] in known_inputs:
            continue
        case_id = f"captured-{str(record.get('trace_id', ''))[:8] or len(known_ids) + 1}"
        while case_id in known_ids:
            case_id += "x"
        known_ids.add(case_id)
        known_inputs.add(record["input"])
        promoted.append(
            GoldenCase(
                id=case_id,
                input=record["input"],
                expected_tools=expected,
                max_tool_calls=max(len(expected) + 2, 3),
                expected_substrings=record.get("expected_substrings", []),
                tags=["captured"],
            )
        )
    if promoted:
        append_cases(dataset, promoted)
    Path(candidates_path).write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in remaining),
        encoding="utf-8",
    )
    return len(promoted)
````

#### `src/tracegate/evaluation/judge.py`

````python
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from tracegate.models import RunResult
from tracegate.semconv import TOOL_STATUS

SYSTEM_PROMPT = (
    "You review the execution trace of a tool-using software agent. "
    "Decide whether the agent got stuck repeating equivalent tool calls, whether it drifted "
    "away from the user's request, and how efficiently it reached its result. "
    "Reply with a single JSON object with exactly these keys: "
    '"loop_detected" (boolean), "trajectory_efficiency_score" (number between 0 and 1), '
    '"goal_drift" (boolean), "rationale" (short string).'
)

CompletionFn = Callable[[str, str], str]


class JudgeError(Exception):
    pass


class JudgeVerdict(BaseModel):
    loop_detected: bool
    trajectory_efficiency_score: float = Field(ge=0, le=1)
    goal_drift: bool = False
    rationale: str = ""


def trajectory_payload(result: RunResult, field_limit: int = 500) -> dict[str, Any]:
    steps = []
    for index, span in enumerate(result.spans):
        if span.kind not in {"TOOL", "LLM"}:
            continue
        entry: dict[str, Any] = {
            "step": index,
            "kind": span.kind,
            "name": span.name,
            "latency_s": round(span.latency_s, 4),
        }
        if span.kind == "TOOL":
            entry["arguments"] = str(span.attributes.get("tool.parameters", ""))[:field_limit]
            entry["result_status"] = span.attributes.get(TOOL_STATUS)
        steps.append(entry)
    return {
        "task": result.input,
        "run_status": result.status,
        "steps": steps,
        "final_answer": (result.output or "")[:field_limit],
    }


def _extract_json(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        raise JudgeError("judge response did not contain a JSON object")
    return raw[start : end + 1]


class TrajectoryJudge:
    def __init__(self, complete: CompletionFn):
        self._complete = complete

    def evaluate(self, result: RunResult) -> JudgeVerdict:
        payload = json.dumps(trajectory_payload(result), ensure_ascii=False)
        raw = self._complete(SYSTEM_PROMPT, payload)
        try:
            return JudgeVerdict.model_validate_json(_extract_json(raw))
        except ValidationError as exc:
            raise JudgeError(f"invalid judge verdict: {exc}") from exc
````

#### `src/tracegate/evaluation/metrics.py`

````python
from __future__ import annotations

from pydantic import BaseModel

from tracegate.evaluation.dataset import GoldenCase
from tracegate.evaluation.judge import JudgeVerdict
from tracegate.models import RunResult

ALIGNED_STATUSES = {"ok", "tool_error"}


class CaseMetrics(BaseModel):
    completed: bool
    tool_selection_correct: bool
    schema_checked: int
    schema_aligned: int
    tool_call_count: int
    within_call_limit: bool
    loop_free: bool
    unhandled_error: bool
    within_budget: bool
    answer_match: bool
    trajectory_efficiency: float
    latency_s: float
    cost_usd: float

    @property
    def schema_alignment(self) -> float:
        if self.schema_checked == 0:
            return 1.0
        return self.schema_aligned / self.schema_checked

    @property
    def passed(self) -> bool:
        return (
            self.completed
            and self.tool_selection_correct
            and self.within_call_limit
            and self.answer_match
        )

    def failures(self) -> list[str]:
        checks = {
            "completed": self.completed,
            "tool_selection_correct": self.tool_selection_correct,
            "within_call_limit": self.within_call_limit,
            "loop_free": self.loop_free,
            "within_budget": self.within_budget,
            "answer_match": self.answer_match,
        }
        failed = [name for name, value in checks.items() if not value]
        if self.unhandled_error:
            failed.append("unhandled_error")
        return failed


class CaseResult(BaseModel):
    case: GoldenCase
    run: RunResult
    metrics: CaseMetrics
    judge: JudgeVerdict | None = None
    judge_error: str | None = None


def compute_metrics(case: GoldenCase, run: RunResult) -> CaseMetrics:
    executed = [call for call in run.tool_calls if call.status != "blocked"]
    invoked = {call.name for call in executed}
    output = (run.output or "").lower()
    expected_count = len(case.expected_tools)
    actual_count = len(run.tool_calls)
    if actual_count == 0:
        efficiency = 1.0 if expected_count == 0 else 0.0
    else:
        efficiency = min(1.0, expected_count / actual_count)
    return CaseMetrics(
        completed=run.status == "success",
        tool_selection_correct=invoked == set(case.expected_tools),
        schema_checked=len(executed),
        schema_aligned=sum(1 for call in executed if call.status in ALIGNED_STATUSES),
        tool_call_count=actual_count,
        within_call_limit=actual_count <= case.max_tool_calls,
        loop_free=run.status not in {"loop_detected", "step_limit"},
        unhandled_error=run.status == "error",
        within_budget=run.status != "budget_exceeded",
        answer_match=all(text.lower() in output for text in case.expected_substrings),
        trajectory_efficiency=efficiency,
        latency_s=run.latency_s,
        cost_usd=run.cost_usd,
    )
````

#### `src/tracegate/evaluation/scorecard.py`

````python
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from tracegate.evaluation.metrics import CaseResult

Verdict = Literal["pass", "pass_with_warnings", "fail"]


class Threshold(BaseModel):
    metric: str
    comparator: Literal["min", "max"]
    value: float
    severity: Literal["block", "warn"]
    action: str


class Check(BaseModel):
    metric: str
    comparator: Literal["min", "max"]
    threshold: float
    actual: float | None
    passed: bool
    severity: Literal["block", "warn"]
    action: str


class Scorecard(BaseModel):
    cases: int
    passed_cases: int
    metrics: dict[str, float]
    checks: list[Check]
    verdict: Verdict

    def to_markdown(self) -> str:
        lines = [
            "# Agent evaluation scorecard",
            "",
            f"Verdict: **{self.verdict.replace('_', ' ').upper()}**",
            "",
            f"Cases: {self.cases}, passed: {self.passed_cases}",
            "",
            "| Metric | Target | Actual | Result | Action on failure |",
            "| --- | --- | --- | --- | --- |",
        ]
        for check in self.checks:
            symbol = ">=" if check.comparator == "min" else "<="
            actual = "n/a" if check.actual is None else f"{check.actual:.4f}"
            if check.passed:
                result = "pass"
            else:
                result = "FAIL" if check.severity == "block" else "WARN"
            lines.append(
                f"| {check.metric} | {symbol} {check.threshold:g} | {actual} | {result} "
                f"| {check.action} |"
            )
        return "\n".join(lines) + "\n"


DEFAULT_THRESHOLDS: list[Threshold] = [
    Threshold(
        metric="tool_selection_accuracy",
        comparator="min",
        value=0.92,
        severity="block",
        action="Block deployment: revise tool names, descriptions and parameter schemas",
    ),
    Threshold(
        metric="schema_alignment",
        comparator="min",
        value=0.92,
        severity="block",
        action="Block deployment: tighten argument schemas or prompt guidance",
    ),
    Threshold(
        metric="unhandled_error_rate",
        comparator="max",
        value=0.0,
        severity="block",
        action="Block deployment: wrap the failing code path",
    ),
    Threshold(
        metric="loop_rate",
        comparator="max",
        value=0.0,
        severity="block",
        action="Block deployment: fix the planning logic that repeats calls",
    ),
    Threshold(
        metric="budget_breach_rate",
        comparator="max",
        value=0.0,
        severity="block",
        action="Block deployment: reduce context growth or raise the budget deliberately",
    ),
    Threshold(
        metric="call_limit_compliance",
        comparator="min",
        value=1.0,
        severity="block",
        action="Block deployment: investigate redundant tool calls",
    ),
    Threshold(
        metric="answer_match_rate",
        comparator="min",
        value=0.9,
        severity="block",
        action="Block deployment: review answer synthesis",
    ),
    Threshold(
        metric="latency_p95_s",
        comparator="max",
        value=12.0,
        severity="warn",
        action="Warning: review dependencies and routing",
    ),
    Threshold(
        metric="mean_trajectory_efficiency",
        comparator="min",
        value=0.7,
        severity="warn",
        action="Warning: review planning prompts and tool granularity",
    ),
    Threshold(
        metric="mean_cost_usd",
        comparator="max",
        value=0.25,
        severity="warn",
        action="Warning: review context size and model choice",
    ),
]


def load_thresholds(path: str | Path) -> list[Threshold]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Threshold.model_validate(entry) for entry in data]


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(math.ceil(quantile * len(ordered)), 1)
    return ordered[rank - 1]


def _rate(flags: Iterable[bool]) -> float:
    items = list(flags)
    return sum(1 for flag in items if flag) / len(items)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def aggregate(results: Sequence[CaseResult]) -> dict[str, float]:
    if not results:
        raise ValueError("no results to aggregate")
    items = [result.metrics for result in results]
    checked = sum(item.schema_checked for item in items)
    aligned = sum(item.schema_aligned for item in items)
    metrics = {
        "pass_rate": _rate(item.passed for item in items),
        "tool_selection_accuracy": _rate(item.tool_selection_correct for item in items),
        "schema_alignment": 1.0 if checked == 0 else aligned / checked,
        "unhandled_error_rate": _rate(item.unhandled_error for item in items),
        "loop_rate": _rate(not item.loop_free for item in items),
        "budget_breach_rate": _rate(not item.within_budget for item in items),
        "call_limit_compliance": _rate(item.within_call_limit for item in items),
        "answer_match_rate": _rate(item.answer_match for item in items),
        "mean_trajectory_efficiency": _mean([item.trajectory_efficiency for item in items]),
        "latency_p95_s": percentile([item.latency_s for item in items], 0.95),
        "mean_cost_usd": _mean([item.cost_usd for item in items]),
    }
    verdicts = [result.judge for result in results if result.judge is not None]
    if verdicts:
        metrics["judge_efficiency"] = _mean([v.trajectory_efficiency_score for v in verdicts])
        metrics["judge_loop_rate"] = _rate(v.loop_detected for v in verdicts)
    return metrics


def evaluate_thresholds(metrics: dict[str, float], thresholds: Sequence[Threshold]) -> list[Check]:
    checks: list[Check] = []
    tolerance = 1e-9
    for threshold in thresholds:
        actual = metrics.get(threshold.metric)
        if actual is None:
            passed = False
        elif threshold.comparator == "min":
            passed = actual >= threshold.value - tolerance
        else:
            passed = actual <= threshold.value + tolerance
        checks.append(
            Check(
                metric=threshold.metric,
                comparator=threshold.comparator,
                threshold=threshold.value,
                actual=actual,
                passed=passed,
                severity=threshold.severity,
                action=threshold.action,
            )
        )
    return checks


def build_scorecard(
    results: Sequence[CaseResult], thresholds: Sequence[Threshold] | None = None
) -> Scorecard:
    metrics = aggregate(results)
    checks = evaluate_thresholds(metrics, DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    failed = [check for check in checks if not check.passed]
    if any(check.severity == "block" for check in failed):
        verdict: Verdict = "fail"
    elif failed:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"
    return Scorecard(
        cases=len(results),
        passed_cases=sum(1 for result in results if result.metrics.passed),
        metrics=metrics,
        checks=checks,
        verdict=verdict,
    )
````

#### `src/tracegate/evaluation/runner.py`

````python
from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

from tracegate.evaluation.dataset import GoldenCase
from tracegate.evaluation.judge import JudgeError, TrajectoryJudge
from tracegate.evaluation.metrics import CaseResult, compute_metrics
from tracegate.evaluation.scorecard import Scorecard, Threshold, build_scorecard
from tracegate.harness import Agent, AgentHarness


class EvaluationReport(BaseModel):
    scorecard: Scorecard
    results: list[CaseResult]

    def failing_cases(self) -> list[CaseResult]:
        return [result for result in self.results if not result.metrics.passed]

    def to_markdown(self) -> str:
        text = self.scorecard.to_markdown()
        failing = self.failing_cases()
        if not failing:
            return text
        lines = [
            "",
            "## Failing cases",
            "",
            "| Case | Run status | Failed checks |",
            "| --- | --- | --- |",
        ]
        for result in failing:
            checks = ", ".join(result.metrics.failures()) or "none"
            lines.append(f"| {result.case.id} | {result.run.status} | {checks} |")
        return text + "\n".join(lines) + "\n"

    def write(self, directory: str | Path) -> None:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "scorecard.json").write_text(
            self.scorecard.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (target / "scorecard.md").write_text(self.to_markdown(), encoding="utf-8")
        with (target / "results.jsonl").open("w", encoding="utf-8") as handle:
            for result in self.results:
                handle.write(result.model_dump_json() + "\n")


class EvaluationRunner:
    def __init__(
        self,
        harness: AgentHarness,
        agent_factory: Callable[[], Agent],
        judge: TrajectoryJudge | None = None,
        concurrency: int = 4,
        thresholds: Sequence[Threshold] | None = None,
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.harness = harness
        self.agent_factory = agent_factory
        self.judge = judge
        self.concurrency = concurrency
        self.thresholds = thresholds

    def _run_case(self, case: GoldenCase) -> CaseResult:
        agent = self.agent_factory()
        run = self.harness.run(agent, case.input, session_id=case.id, metadata={"case_id": case.id})
        metrics = compute_metrics(case, run)
        verdict = None
        judge_error = None
        if self.judge is not None:
            try:
                verdict = self.judge.evaluate(run)
            except JudgeError as exc:
                judge_error = str(exc)
        return CaseResult(
            case=case, run=run, metrics=metrics, judge=verdict, judge_error=judge_error
        )

    def run(self, cases: Sequence[GoldenCase]) -> EvaluationReport:
        if not cases:
            raise ValueError("no cases to evaluate")
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            results = list(pool.map(self._run_case, cases))
        self.harness.flush()
        return EvaluationReport(
            scorecard=build_scorecard(results, self.thresholds), results=results
        )
````

#### `src/tracegate/evaluation/rag.py`

````python
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
````

### 10.2 Dataset, infrastructure and examples

#### `datasets/golden.jsonl`

````json
{"id": "q3-revenue", "input": "Fetch revenue numbers for Q3", "expected_tools": ["query_database"], "max_tool_calls": 3, "expected_substrings": ["412000"], "tags": ["database"]}
{"id": "q1-sales", "input": "Pull Q1 sales from the database", "expected_tools": ["query_database"], "max_tool_calls": 3, "expected_substrings": ["300000"], "tags": ["database"]}
{"id": "all-quarters", "input": "Show sales by quarter", "expected_tools": ["query_database"], "max_tool_calls": 3, "expected_substrings": ["Q1", "Q2", "Q3"], "tags": ["database"]}
{"id": "alert-email", "input": "Send alert email to admin", "expected_tools": ["send_email"], "max_tool_calls": 3, "expected_substrings": ["queued", "admin@example.com"], "tags": ["email"]}
{"id": "docs-rollback", "input": "Search the docs for the rollback procedure", "expected_tools": ["search_documents"], "max_tool_calls": 3, "expected_substrings": ["rollback-guide"], "tags": ["search"]}
{"id": "query-and-email", "input": "Query Q2 revenue and email the result to ops@example.com", "expected_tools": ["query_database", "send_email"], "max_tool_calls": 4, "expected_substrings": ["347000", "ops@example.com"], "tags": ["multi-step"]}
{"id": "docs-and-notify", "input": "Search docs for the incident runbook and notify ops@example.com", "expected_tools": ["search_documents", "send_email"], "max_tool_calls": 4, "expected_substrings": ["incident-runbook", "queued"], "tags": ["multi-step"]}
{"id": "out-of-scope", "input": "What is the weather like in Paris", "expected_tools": [], "max_tool_calls": 0, "expected_substrings": ["only help"], "tags": ["refusal"]}
````

#### `Dockerfile`

````dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[otlp]"
COPY datasets ./datasets
ENTRYPOINT ["tracegate"]
CMD ["eval", "--dataset", "datasets/golden.jsonl", "--out", "reports"]
````

#### `docker-compose.yml`

````yaml
services:
  database:
    image: postgres:16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: phoenix
    volumes:
      - phoenix-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d phoenix"]
      interval: 5s
      timeout: 5s
      retries: 10

  phoenix:
    image: arizephoenix/phoenix:latest
    depends_on:
      database:
        condition: service_healthy
    ports:
      - "6006:6006"
      - "4317:4317"
    environment:
      PHOENIX_SQL_DATABASE_URL: postgresql://postgres:postgres@database:5432/phoenix

volumes:
  phoenix-data:
````

#### `.github/workflows/ci.yml`

````yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  verify:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip
      - run: pip install -e ".[dev,otlp]"
      - run: ruff check src tests examples
      - run: ruff format --check src tests examples
      - run: pytest --cov=tracegate

  release-gate:
    runs-on: ubuntu-latest
    needs: verify
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e ".[otlp]"
      - run: tracegate eval --dataset datasets/golden.jsonl --out reports
      - if: always()
        run: cat reports/scorecard.md >> "$GITHUB_STEP_SUMMARY"
      - if: always()
        uses: actions/upload-artifact@v4
        with:
          name: scorecard
          path: reports
````

#### `Makefile`

````makefile
.PHONY: install lint format test eval up down

install:
	pip install -e ".[dev,otlp]"

lint:
	ruff check src tests examples
	ruff format --check src tests examples

format:
	ruff check --fix src tests examples
	ruff format src tests examples

test:
	pytest --cov=tracegate --cov-report=term-missing

eval:
	tracegate eval --dataset datasets/golden.jsonl --out reports

up:
	docker compose up -d

down:
	docker compose down
````

#### `.env.example`

````
TRACEGATE_SERVICE_NAME=tracegate
TRACEGATE_PROJECT_NAME=tracegate
TRACEGATE_OTLP_ENDPOINT=http://localhost:6006/v1/traces
TRACEGATE_OTLP_HEADERS=
TRACEGATE_CONSOLE_EXPORT=false
TRACEGATE_BUDGET_USD=2.0
TRACEGATE_MAX_STEPS=25
TRACEGATE_LOOP_REPEATS=3
TRACEGATE_LOOP_MAX_PERIOD=3
TRACEGATE_MAX_PAYLOAD_CHARS=8000
TRACEGATE_CAPTURE_BUDGET_RATIO=0.8
TRACEGATE_CAPTURE_STEP_RATIO=0.8
TRACEGATE_PRICING_FILE=
````

#### `.gitignore`

````
__pycache__/
*.py[cod]
*.egg-info/
.venv/
.env
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
reports/
dist/
build/
````

#### `examples/quickstart.py`

````python
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
````

#### `examples/custom_agent.py`

````python
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
````

#### `examples/openai_agent.py`

````python
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
````

#### `examples/rag_eval.py`

````python
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
````

#### `examples/thresholds.json`

````json
[
  {
    "metric": "tool_selection_accuracy",
    "comparator": "min",
    "value": 0.95,
    "severity": "block",
    "action": "Block deployment: revise tool descriptions"
  },
  {
    "metric": "latency_p95_s",
    "comparator": "max",
    "value": 8.0,
    "severity": "warn",
    "action": "Warning: profile slow tools"
  },
  {
    "metric": "mean_cost_usd",
    "comparator": "max",
    "value": 0.05,
    "severity": "block",
    "action": "Block deployment: cost per run is over target"
  }
]
````

#### `examples/prices.json`

````json
{
  "my-fine-tune": {
    "input_per_million": 4.0,
    "output_per_million": 12.0
  }
}
````


## 11. Tests

Run the suite with

```
pytest --cov=tracegate --cov-report=term-missing
```

The tests cover:

- price lookup, prefix matching, price files, cost accumulation and the budget circuit breaker;
- exact, reordered and alternating loop detection, the step limit and argument validation of the guard;
- tool registration, structured argument errors, handler failure containment, SQL write rejection at both the model and connection level, and the bundled tools;
- trace structure (parent links, attributes, token and cost totals), statuses for loop, step limit, budget, crash, unknown tool and unpriced model, metadata handling, payload clipping, isolation of concurrent runs, collector cleanup, a failing sink and console export to standard error;
- OTLP export against a local HTTP receiver, checking path, headers, content type and span content;
- case metrics, scorecard verdicts, warnings, missing metrics, report files, percentile, dataset and threshold file validation;
- the judge, including fenced and malformed replies, and judge failure inside the runner;
- candidate capture, deduplication and promotion;
- every CLI command and exit code, including `python -m tracegate`;
- the OpenAI tool loop, `openai_invoke`, `anthropic_invoke` and `openai_completion` against fake clients;
- Ragas aggregation with an offline Ragas metric. These tests are skipped when Ragas is not installed.

#### `tests/__init__.py`

````python

````

#### `tests/conftest.py`

````python
from __future__ import annotations

import pytest

from tracegate import AgentHarness, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def harness(settings: Settings):
    instance = AgentHarness(settings)
    yield instance
    instance.close()
````

#### `tests/policies.py`

````python
from __future__ import annotations

from typing import Any

from tracegate.reference import keyword_policy

QUERY = {"query_string": "SELECT SUM(amount) AS total FROM revenue", "row_limit": 10}
DOCS = {"query": "rollback procedure", "top_k": 2}


def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"action": "tool", "tool": name, "arguments": arguments}


def looping_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return tool("query_database", QUERY)


def alternating_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count % 2 == 0:
        return tool("query_database", QUERY)
    return tool("search_documents", DOCS)


def drifting_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    return tool(
        "query_database",
        {"query_string": f"SELECT {count} AS step_value FROM revenue", "row_limit": 5},
    )


def fumbling_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count == 0:
        return tool("query_database", {"query_string": "SELECT 1", "row_limit": 5})
    if count == 1:
        return tool(
            "query_database",
            {"query_string": "SELECT SUM(amount) AS total FROM revenue WHERE quarter = 'Q3'"},
        )
    return {"action": "final", "answer": "total 412000"}


def unknown_tool_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count == 0:
        return tool("delete_everything", {})
    return {"action": "final", "answer": "gave up"}


def crashing_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    raise RuntimeError("policy exploded")


def wasteful_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count < 3:
        return tool(
            "query_database",
            {"query_string": f"SELECT {count + 1} AS n FROM revenue", "row_limit": 1},
        )
    return {"action": "final", "answer": "done"}


__all__ = [
    "alternating_policy",
    "crashing_policy",
    "drifting_policy",
    "fumbling_policy",
    "keyword_policy",
    "looping_policy",
    "unknown_tool_policy",
    "wasteful_policy",
]
````

#### `tests/test_pricing_budget.py`

````python
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
````

#### `tests/test_guardrails.py`

````python
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
````

#### `tests/test_tools.py`

````python
from __future__ import annotations

import sqlite3

import pytest
from pydantic import BaseModel, Field

from tracegate import ToolRegistry
from tracegate.reference import DatabaseQuery, build_database, build_registry


class Add(BaseModel):
    a: int
    b: int = Field(default=1, ge=0)


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register("add", Add, "Add two numbers")
    def add(args: Add) -> dict:
        return {"sum": args.a + args.b}

    @registry.register("explode", Add)
    def explode(args: Add) -> None:
        raise RuntimeError("boom")

    return registry


def test_successful_execution():
    result = make_registry().execute("add", {"a": 2, "b": 3})
    assert result.ok
    assert result.output == {"sum": 5}


def test_invalid_arguments_return_structured_errors():
    result = make_registry().execute("add", {"a": "x", "b": -1})
    assert result.status == "argument_error"
    assert result.retryable
    locations = {tuple(error["loc"]) for error in result.errors}
    assert ("a",) in locations
    assert ("b",) in locations
    assert all("url" not in error and "input" not in error for error in result.errors)


def test_non_mapping_arguments_are_rejected_cleanly():
    assert make_registry().execute("add", "not a dict").status == "argument_error"
    assert make_registry().execute("add", None).status == "argument_error"


def test_unknown_tool():
    result = make_registry().execute("missing", {})
    assert result.status == "unknown_tool"
    assert result.retryable


def test_handler_exceptions_do_not_escape():
    result = make_registry().execute("explode", {"a": 1})
    assert result.status == "tool_error"
    assert result.errors[0]["type"] == "RuntimeError"
    assert not result.retryable


def test_duplicate_registration_is_rejected():
    registry = make_registry()
    with pytest.raises(ValueError):
        registry.register("add", Add)(lambda args: None)


def test_schemas_expose_json_schema():
    schemas = make_registry().schemas()
    assert {schema["name"] for schema in schemas} == {"add", "explode"}
    add = next(schema for schema in schemas if schema["name"] == "add")
    assert add["parameters"]["properties"]["a"]["type"] == "integer"
    assert "add" in make_registry()


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM revenue WHERE 1=1",
        "SELECT 1; DROP TABLE revenue",
        "UPDATE revenue SET amount = 0",
    ],
)
def test_database_query_rejects_writes(query):
    registry = build_registry(build_database(), [])
    assert registry.execute("query_database", {"query_string": query}).status == "argument_error"


def test_database_query_runs_and_respects_row_limit():
    registry = build_registry(build_database(), [])
    result = registry.execute(
        "query_database",
        {"query_string": "SELECT region, amount FROM revenue ORDER BY amount", "row_limit": 2},
    )
    assert result.ok
    assert len(result.output["rows"]) == 2
    assert result.output["columns"] == ["region", "amount"]


def test_database_is_read_only_at_the_connection_level():
    connection = build_database()
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM revenue")


def test_database_query_model_normalizes_statement():
    assert DatabaseQuery(query_string="SELECT 1 FROM revenue;").query_string == (
        "SELECT 1 FROM revenue"
    )


def test_email_and_search_tools():
    outbox: list = []
    registry = build_registry(build_database(), outbox)
    bad = registry.execute("send_email", {"recipient": "nope", "subject": "s", "body": "b"})
    assert bad.status == "argument_error"
    good = registry.execute("send_email", {"recipient": "a@b.co", "subject": "s", "body": "b"})
    assert good.output["message_id"] == "msg-1"
    assert len(outbox) == 1
    found = registry.execute("search_documents", {"query": "rollback procedure"})
    assert found.output[0]["id"] == "rollback-guide"
    assert registry.execute("search_documents", {"query": "zzzzqq"}).output == []
````

#### `tests/test_harness.py`

````python
from __future__ import annotations

import pytest

from tests import policies
from tracegate import AgentHarness, FunctionAgent, LLMResponse, Settings, ToolRegistry
from tracegate.reference import ReferenceAgent


def run(harness: AgentHarness, prompt: str, policy=policies.keyword_policy):
    return harness.run(ReferenceAgent(policy=policy), prompt, session_id="s-1")


def test_successful_run_produces_a_connected_trace(harness):
    result = run(harness, "Fetch revenue numbers for Q3")
    assert result.status == "success"
    assert "412000" in result.output
    assert result.llm_calls == 2
    assert [call.name for call in result.tool_calls] == ["query_database"]
    assert result.cost_usd > 0
    assert result.input_tokens > 0 and result.output_tokens > 0
    kinds = [span.kind for span in result.spans]
    assert kinds.count("AGENT") == 1
    assert kinds.count("LLM") == 2
    assert kinds.count("TOOL") == 1
    root = next(span for span in result.spans if span.kind == "AGENT")
    assert root.parent_id is None
    assert all(span.trace_id == result.trace_id for span in result.spans)
    children = [span for span in result.spans if span.kind != "AGENT"]
    assert all(span.parent_id == root.span_id for span in children)
    assert root.attributes["session.id"] == "s-1"
    assert root.attributes["agent.run.status"] == "success"
    assert root.attributes["agent.run.llm_calls"] == 2


def test_llm_spans_follow_openinference_attributes(harness):
    result = run(harness, "Fetch revenue numbers for Q3")
    llm = next(span for span in result.spans if span.kind == "LLM")
    assert llm.attributes["llm.model_name"] == "mock-llm"
    assert llm.attributes["llm.token_count.total"] == (
        llm.attributes["llm.token_count.prompt"] + llm.attributes["llm.token_count.completion"]
    )
    tool = next(span for span in result.spans if span.kind == "TOOL")
    assert tool.attributes["tool.name"] == "query_database"
    assert tool.attributes["agent.tool.status"] == "ok"


def test_loop_is_stopped_and_reported(harness):
    result = run(harness, "anything", policies.looping_policy)
    assert result.status == "loop_detected"
    assert result.error and "cycle" in result.error
    assert [call.status for call in result.tool_calls] == ["ok", "ok", "blocked"]
    assert result.spans[0].status == "ERROR"


def test_alternating_loop_is_detected(harness):
    result = run(harness, "anything", policies.alternating_policy)
    assert result.status == "loop_detected"
    assert len(result.tool_calls) == 6


def test_step_limit_stops_runs_that_vary_their_calls():
    harness = AgentHarness(Settings(_env_file=None, max_steps=4))
    try:
        result = run(harness, "anything", policies.drifting_policy)
    finally:
        harness.close()
    assert result.status == "step_limit"
    assert len(result.tool_calls) == 5
    assert result.tool_calls[-1].status == "blocked"


def test_budget_circuit_breaker():
    harness = AgentHarness(Settings(_env_file=None, budget_usd=0.000001))
    try:
        result = run(harness, "Fetch revenue numbers for Q3")
    finally:
        harness.close()
    assert result.status == "budget_exceeded"
    assert result.cost_usd > 0.000001
    assert result.tool_calls == []


def test_agent_exceptions_never_escape(harness):
    result = run(harness, "anything", policies.crashing_policy)
    assert result.status == "error"
    assert "policy exploded" in result.error
    assert result.output is None


def test_argument_errors_are_recorded_and_recoverable(harness):
    result = run(harness, "anything", policies.fumbling_policy)
    assert result.status == "success"
    assert [call.status for call in result.tool_calls] == ["argument_error", "ok"]
    failed = next(span for span in result.spans if span.kind == "TOOL")
    assert failed.status == "ERROR"


def test_unknown_tools_are_reported(harness):
    result = run(harness, "anything", policies.unknown_tool_policy)
    assert result.status == "success"
    assert result.tool_calls[0].status == "unknown_tool"


def test_unpriced_model_fails_the_run_loudly(harness):
    agent = ReferenceAgent(model="mystery-model")
    result = harness.run(agent, "Fetch revenue numbers for Q3")
    assert result.status == "error"
    assert "mystery-model" in result.error


def test_function_agent_and_metadata(harness):
    registry = ToolRegistry()

    def fn(ctx, text):
        response = ctx.llm(
            "mock-llm",
            [{"role": "user", "content": text}],
            lambda: LLMResponse(text=text.upper(), input_tokens=3, output_tokens=2),
        )
        return response.text

    result = harness.run(
        FunctionAgent(registry, fn),
        "hello",
        metadata={"tenant": "acme", "attempt": 2, "tags": ["a"]},
    )
    assert result.output == "HELLO"
    root = next(span for span in result.spans if span.kind == "AGENT")
    assert root.attributes["metadata.tenant"] == "acme"
    assert root.attributes["metadata.attempt"] == 2
    assert root.attributes["metadata.tags"] == '["a"]'


def test_non_string_agent_output_is_serialized(harness):
    agent = FunctionAgent(ToolRegistry(), lambda ctx, text: {"answer": 1})
    assert harness.run(agent, "x").output == '{"answer": 1}'


def test_payloads_are_clipped():
    harness = AgentHarness(Settings(_env_file=None, max_payload_chars=64))
    try:
        agent = FunctionAgent(ToolRegistry(), lambda ctx, text: text)
        result = harness.run(agent, "x" * 500)
    finally:
        harness.close()
    root = result.spans[0]
    assert len(root.attributes["input.value"]) == 64
    assert root.attributes["input.value"].endswith("...")


def test_concurrent_runs_keep_traces_isolated(harness):
    from concurrent.futures import ThreadPoolExecutor

    prompts = ["Fetch revenue numbers for Q3", "Send alert email to admin"] * 8
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda prompt: run(harness, prompt), prompts))
    assert len({result.trace_id for result in results}) == len(prompts)
    for result in results:
        assert all(span.trace_id == result.trace_id for span in result.spans)
        assert len([span for span in result.spans if span.kind == "TOOL"]) == 1
    assert [result.tool_calls[0].name for result in results[:2]] == [
        "query_database",
        "send_email",
    ]


def test_collector_does_not_retain_finished_traces(harness):
    run(harness, "Fetch revenue numbers for Q3")
    assert harness.telemetry.collector._spans == {}


def test_failing_sink_does_not_break_the_run(settings):
    class BrokenSink:
        def consider(self, result):
            raise OSError("disk full")

    harness = AgentHarness(settings, sink=BrokenSink())
    try:
        result = run(harness, "Fetch revenue numbers for Q3")
    finally:
        harness.close()
    assert result.status == "success"


def test_console_exporter_writes_to_stderr(capsys):
    harness = AgentHarness(Settings(_env_file=None, console_export=True))
    try:
        run(harness, "Send alert email to admin")
        harness.flush()
    finally:
        harness.close()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "openinference.span.kind" in captured.err


def test_otlp_headers_parse_and_reject_garbage():
    settings = Settings(_env_file=None, otlp_headers="Authorization=Basic abc==, x-key=1")
    assert settings.otlp_header_map == {"Authorization": "Basic abc==", "x-key": "1"}
    broken = Settings(_env_file=None, otlp_headers="broken")
    with pytest.raises(ValueError):
        _ = broken.otlp_header_map


def test_blank_endpoint_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("TRACEGATE_OTLP_ENDPOINT", "")
    assert Settings(_env_file=None).otlp_endpoint is None


def test_otlp_export_reaches_a_collector_with_headers():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "type": self.headers.get("Content-Type"),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        _env_file=None,
        otlp_endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
        otlp_headers="Authorization=Basic dGVzdA==",
    )
    harness = AgentHarness(settings)
    try:
        run(harness, "Send alert email to admin")
        assert harness.flush()
    finally:
        harness.close()
        server.shutdown()
        server.server_close()
    assert received
    first = received[0]
    assert first["path"] == "/v1/traces"
    assert first["auth"] == "Basic dGVzdA=="
    assert first["type"] == "application/x-protobuf"
    assert b"agent.run" in first["body"]
    assert b"tool.send_email" in first["body"]
    assert b"openinference.span.kind" in first["body"]
````

#### `tests/test_evaluation.py`

````python
from __future__ import annotations

import json

import pytest

from tests import policies
from tracegate import AgentHarness, Settings
from tracegate.evaluation import (
    EvaluationRunner,
    GoldenCase,
    Threshold,
    build_scorecard,
    compute_metrics,
    load_dataset,
    load_thresholds,
)
from tracegate.evaluation.scorecard import percentile
from tracegate.reference import ReferenceAgent


def case(**overrides) -> GoldenCase:
    base = {
        "id": "c1",
        "input": "Fetch revenue numbers for Q3",
        "expected_tools": ["query_database"],
        "max_tool_calls": 3,
        "expected_substrings": ["412000"],
    }
    base.update(overrides)
    return GoldenCase(**base)


def evaluate(harness, golden: GoldenCase, policy=policies.keyword_policy, **runner_kwargs):
    runner = EvaluationRunner(
        harness, lambda: ReferenceAgent(policy=policy), concurrency=1, **runner_kwargs
    )
    return runner.run([golden])


def test_golden_dataset_passes_every_release_gate(harness):
    cases = load_dataset("datasets/golden.jsonl")
    report = EvaluationRunner(harness, ReferenceAgent, concurrency=4).run(cases)
    assert report.scorecard.verdict == "pass"
    assert report.scorecard.passed_cases == len(cases)
    assert report.scorecard.metrics["tool_selection_accuracy"] == 1.0
    assert [result.case.id for result in report.results] == [c.id for c in cases]


def test_perfect_case_metrics(harness):
    metrics = evaluate(harness, case()).results[0].metrics
    assert metrics.passed
    assert metrics.schema_alignment == 1.0
    assert metrics.trajectory_efficiency == 1.0
    assert metrics.failures() == []


def test_wrong_tool_fails_selection(harness):
    report = evaluate(harness, case(expected_tools=["send_email"]))
    assert not report.results[0].metrics.tool_selection_correct
    assert report.scorecard.verdict == "fail"
    assert "tool_selection_correct" in report.results[0].metrics.failures()


def test_loop_is_reported_as_blocking(harness):
    report = evaluate(harness, case(max_tool_calls=10), policies.looping_policy)
    metrics = report.results[0].metrics
    assert not metrics.loop_free
    assert not metrics.completed
    assert report.scorecard.metrics["loop_rate"] == 1.0
    assert report.scorecard.verdict == "fail"
    assert "loop_free" in metrics.failures()


def test_unhandled_error_and_budget_flags():
    crashing = AgentHarness(Settings(_env_file=None))
    try:
        report = evaluate(crashing, case(), policies.crashing_policy)
    finally:
        crashing.close()
    assert report.results[0].metrics.unhandled_error
    assert report.scorecard.metrics["unhandled_error_rate"] == 1.0
    tight = AgentHarness(Settings(_env_file=None, budget_usd=0.000001))
    try:
        report = evaluate(tight, case())
    finally:
        tight.close()
    assert not report.results[0].metrics.within_budget
    assert report.scorecard.metrics["budget_breach_rate"] == 1.0


def test_schema_alignment_counts_argument_errors(harness):
    report = evaluate(harness, case(max_tool_calls=5), policies.fumbling_policy)
    metrics = report.results[0].metrics
    assert (metrics.schema_checked, metrics.schema_aligned) == (2, 1)
    assert report.scorecard.metrics["schema_alignment"] == 0.5
    assert report.scorecard.verdict == "fail"


def test_call_limit_and_efficiency(harness):
    report = evaluate(
        harness, case(expected_substrings=[], max_tool_calls=2), policies.wasteful_policy
    )
    metrics = report.results[0].metrics
    assert metrics.tool_call_count == 3
    assert not metrics.within_call_limit
    assert metrics.trajectory_efficiency == pytest.approx(1 / 3)
    assert not metrics.passed


def test_out_of_scope_case_expects_no_tools(harness):
    golden = case(
        input="What is the weather like in Paris",
        expected_tools=[],
        max_tool_calls=0,
        expected_substrings=["only help"],
    )
    metrics = evaluate(harness, golden).results[0].metrics
    assert metrics.passed
    assert metrics.trajectory_efficiency == 1.0


def test_warnings_do_not_block(harness):
    thresholds = [
        Threshold(metric="mean_cost_usd", comparator="max", value=0.0, severity="warn", action="x")
    ]
    report = evaluate(harness, case(), thresholds=thresholds)
    assert report.scorecard.verdict == "pass_with_warnings"


def test_missing_metric_fails_its_check(harness):
    thresholds = [
        Threshold(
            metric="judge_efficiency", comparator="min", value=0.5, severity="block", action="x"
        )
    ]
    report = evaluate(harness, case(), thresholds=thresholds)
    assert report.scorecard.checks[0].actual is None
    assert report.scorecard.verdict == "fail"


def test_report_files(harness, tmp_path):
    report = evaluate(harness, case(expected_tools=["send_email"]))
    report.write(tmp_path / "out")
    scorecard = json.loads((tmp_path / "out" / "scorecard.json").read_text())
    assert scorecard["verdict"] == "fail"
    markdown = (tmp_path / "out" / "scorecard.md").read_text()
    assert "## Failing cases" in markdown
    lines = (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["case"]["id"] == "c1"


def test_percentile_uses_nearest_rank():
    assert percentile([], 0.95) == 0.0
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([float(n) for n in range(1, 21)], 0.95) == 19.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0


def test_dataset_validation(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a", "input": "x"}\n{"id": "a", "input": "y"}\n')
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(path)
    path.write_text('{"input": "x"}\n')
    with pytest.raises(ValueError, match=":1:"):
        load_dataset(path)
    path.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        load_dataset(path)


def test_threshold_file_round_trip(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(
        json.dumps(
            [
                {
                    "metric": "pass_rate",
                    "comparator": "min",
                    "value": 1.0,
                    "severity": "block",
                    "action": "stop",
                }
            ]
        )
    )
    assert load_thresholds(path)[0].metric == "pass_rate"


def test_runner_rejects_bad_input(harness):
    with pytest.raises(ValueError):
        EvaluationRunner(harness, ReferenceAgent, concurrency=0)
    with pytest.raises(ValueError):
        EvaluationRunner(harness, ReferenceAgent).run([])
    with pytest.raises(ValueError):
        build_scorecard([])


def test_compute_metrics_ignores_blocked_calls_for_selection(harness):
    run = harness.run(ReferenceAgent(policy=policies.looping_policy), "anything")
    metrics = compute_metrics(case(expected_substrings=[]), run)
    assert metrics.tool_selection_correct
    assert not metrics.loop_free
````

#### `tests/test_judge_capture_cli.py`

````python
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests import policies
from tracegate import AgentHarness, CaptureSink, Settings
from tracegate.cli import load_factory, main
from tracegate.evaluation import (
    EvaluationRunner,
    GoldenCase,
    JudgeError,
    TrajectoryJudge,
    load_dataset,
    promote_candidates,
)
from tracegate.evaluation.judge import trajectory_payload
from tracegate.integrations import anthropic_invoke, openai_completion, openai_invoke
from tracegate.openai_agent import OpenAIToolAgent
from tracegate.reference import ReferenceAgent, build_database, build_registry


def sample_run(harness, prompt="Fetch revenue numbers for Q3", policy=policies.keyword_policy):
    return harness.run(ReferenceAgent(policy=policy), prompt)


def test_judge_parses_fenced_json_and_sends_a_compact_trajectory(harness):
    seen = {}

    def complete(system, user):
        seen["system"], seen["user"] = system, user
        return '```json\n{"loop_detected": false, "trajectory_efficiency_score": 0.9}\n```'

    verdict = TrajectoryJudge(complete).evaluate(sample_run(harness))
    assert verdict.trajectory_efficiency_score == 0.9
    assert not verdict.goal_drift
    payload = json.loads(seen["user"])
    assert payload["task"] == "Fetch revenue numbers for Q3"
    assert [step["kind"] for step in payload["steps"]] == ["LLM", "TOOL", "LLM"]
    assert payload["steps"][1]["result_status"] == "ok"
    assert "JSON" in seen["system"]


def test_judge_rejects_malformed_output(harness):
    run = sample_run(harness)
    with pytest.raises(JudgeError):
        TrajectoryJudge(lambda system, user: "no json here").evaluate(run)
    with pytest.raises(JudgeError):
        TrajectoryJudge(
            lambda system, user: '{"loop_detected": false, "trajectory_efficiency_score": 4}'
        ).evaluate(run)


def test_trajectory_payload_clips_long_fields(harness):
    run = sample_run(harness)
    payload = trajectory_payload(run, field_limit=10)
    assert len(payload["steps"][1]["arguments"]) <= 10


def test_runner_aggregates_judge_scores_and_survives_judge_failures(harness):
    golden = GoldenCase(
        id="c",
        input="Fetch revenue numbers for Q3",
        expected_tools=["query_database"],
        expected_substrings=["412000"],
    )
    good = TrajectoryJudge(
        lambda s, u: '{"loop_detected": false, "trajectory_efficiency_score": 0.8}'
    )
    report = EvaluationRunner(harness, ReferenceAgent, judge=good, concurrency=1).run([golden])
    assert report.scorecard.metrics["judge_efficiency"] == pytest.approx(0.8)
    assert report.scorecard.metrics["judge_loop_rate"] == 0.0
    broken = TrajectoryJudge(lambda s, u: "nonsense")
    report = EvaluationRunner(harness, ReferenceAgent, judge=broken, concurrency=1).run([golden])
    assert report.results[0].judge is None
    assert report.results[0].judge_error
    assert "judge_efficiency" not in report.scorecard.metrics


def test_capture_flags_failures_and_deduplicates(settings, tmp_path):
    sink = CaptureSink(tmp_path / "candidates.jsonl", settings)
    harness = AgentHarness(settings, sink=sink)
    try:
        sample_run(harness)
        sample_run(harness, "anything", policies.looping_policy)
        sample_run(harness, "anything", policies.looping_policy)
        sample_run(harness, "fumble", policies.fumbling_policy)
    finally:
        harness.close()
    records = [
        json.loads(line) for line in (tmp_path / "candidates.jsonl").read_text().splitlines()
    ]
    assert len(records) == 2
    assert records[0]["reasons"] == ["status:loop_detected"]
    assert records[0]["observed_tools"] == ["query_database"] * 3
    assert records[0]["expected_tools"] is None
    assert records[1]["reasons"] == ["argument_errors"]
    reopened = CaptureSink(tmp_path / "candidates.jsonl", settings)
    assert reopened._seen == {"anything", "fumble"}


def test_capture_flags_step_pressure(tmp_path):
    settings = Settings(_env_file=None, max_steps=3)
    sink = CaptureSink(tmp_path / "c.jsonl", settings)
    harness = AgentHarness(settings, sink=sink)
    try:
        result = sample_run(harness, "anything", policies.wasteful_policy)
    finally:
        harness.close()
    assert result.status == "success"
    assert sink.reasons(result) == ["high_step_count"]


def test_capture_flags_budget_pressure(tmp_path):
    settings = Settings(_env_file=None, budget_usd=0.000001)
    sink = CaptureSink(tmp_path / "c.jsonl", settings)
    harness = AgentHarness(settings, sink=sink)
    try:
        result = sample_run(harness)
    finally:
        harness.close()
    assert sink.reasons(result) == ["status:budget_exceeded", "budget_pressure"]


def test_promotion_moves_reviewed_candidates(tmp_path):
    candidates = tmp_path / "candidates.jsonl"
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text(
        '{"id": "base", "input": "Send alert email to admin", "expected_tools": ["send_email"]}\n'
    )
    rows = [
        {"input": "new reviewed", "trace_id": "abcdef123456", "expected_tools": ["query_database"]},
        {"input": "still pending", "trace_id": "999", "expected_tools": None},
        {"input": "Send alert email to admin", "trace_id": "dup", "expected_tools": ["send_email"]},
    ]
    candidates.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert promote_candidates(candidates, dataset) == 1
    cases = load_dataset(dataset)
    assert [item.id for item in cases] == ["base", "captured-abcdef12"]
    assert cases[1].max_tool_calls == 3
    remaining = [json.loads(line) for line in candidates.read_text().splitlines()]
    assert [row["input"] for row in remaining] == ["still pending"]


def test_cli_eval_passes_and_writes_reports(tmp_path, capsys):
    code = main(["eval", "--dataset", "datasets/golden.jsonl", "--out", str(tmp_path / "r")])
    assert code == 0
    assert "Verdict: **PASS**" in capsys.readouterr().out
    assert (tmp_path / "r" / "scorecard.json").exists()


def test_cli_eval_fails_with_exit_code_one(tmp_path, capsys):
    dataset = tmp_path / "bad.jsonl"
    dataset.write_text(
        json.dumps(
            {"id": "x", "input": "Send alert email to admin", "expected_tools": ["query_database"]}
        )
        + "\n"
    )
    assert main(["eval", "--dataset", str(dataset), "--out", str(tmp_path / "r")]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_eval_with_thresholds_capture_and_custom_agent(tmp_path):
    thresholds = tmp_path / "t.json"
    thresholds.write_text(
        json.dumps(
            [
                {
                    "metric": "pass_rate",
                    "comparator": "min",
                    "value": 1.0,
                    "severity": "block",
                    "action": "a",
                }
            ]
        )
    )
    code = main(
        [
            "eval",
            "--dataset",
            "datasets/golden.jsonl",
            "--out",
            str(tmp_path / "r"),
            "--thresholds",
            str(thresholds),
            "--capture",
            str(tmp_path / "cand.jsonl"),
            "--agent",
            "tracegate.reference:create_agent",
            "--concurrency",
            "2",
        ]
    )
    assert code == 0
    assert not (tmp_path / "cand.jsonl").exists()


def test_cli_run_prints_result(capsys):
    assert main(["run", "Send alert email to admin"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert "spans" not in payload
    assert main(["run", "Send alert email to admin", "--spans"]) == 0
    assert json.loads(capsys.readouterr().out)["spans"]


def test_cli_promote(tmp_path, capsys):
    candidates = tmp_path / "c.jsonl"
    dataset = tmp_path / "d.jsonl"
    candidates.write_text(json.dumps({"input": "q", "trace_id": "t1", "expected_tools": []}) + "\n")
    assert main(["promote", "--candidates", str(candidates), "--dataset", str(dataset)]) == 0
    assert "promoted 1" in capsys.readouterr().out


def test_cli_reports_configuration_errors(tmp_path, capsys):
    assert main(["eval", "--dataset", str(tmp_path / "missing.jsonl")]) == 2
    assert main(["run", "x", "--agent", "badspec"]) == 2
    assert main(["run", "x", "--agent", "tracegate.reference:missing"]) == 2
    assert "error:" in capsys.readouterr().err


def test_load_factory_default():
    assert load_factory(None)().tools.names() == [
        "query_database",
        "send_email",
        "search_documents",
    ]


def make_openai_response(content=None, tool_calls=None, prompt=10, completion=5):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def make_tool_call(identifier, name, arguments):
    return SimpleNamespace(id=identifier, function=SimpleNamespace(name=name, arguments=arguments))


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def test_openai_tool_agent_round_trip(harness):
    registry = build_registry(build_database(), [])
    client = FakeOpenAI(
        [
            make_openai_response(
                tool_calls=[
                    make_tool_call(
                        "call_1",
                        "query_database",
                        json.dumps({"query_string": "SELECT SUM(amount) AS total FROM revenue"}),
                    ),
                    make_tool_call("call_2", "query_database", "{not json"),
                ]
            ),
            make_openai_response(content="The total is 1059000."),
        ]
    )
    agent = OpenAIToolAgent(registry, client, "gpt-4o-mini")
    result = harness.run(agent, "Total revenue?")
    assert result.status == "success"
    assert result.output == "The total is 1059000."
    assert [call.status for call in result.tool_calls] == ["ok", "argument_error"]
    assert result.llm_calls == 2
    assert result.input_tokens == 20 and result.output_tokens == 10
    first, second = client.requests
    assert first["tools"][0]["function"]["name"] == "query_database"
    roles = [message["role"] for message in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    assert second["messages"][3]["tool_call_id"] == "call_1"


def test_openai_invoke_requires_usage():
    client = FakeOpenAI(
        [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="x"))])]
    )
    with pytest.raises(ValueError):
        openai_invoke(client, "gpt-4o", [])()


def test_anthropic_invoke_collects_text_blocks():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="hello "),
                SimpleNamespace(type="tool_use"),
                SimpleNamespace(type="text", text="world"),
            ],
            usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    response = anthropic_invoke(
        client, "claude-3-5-haiku", [{"role": "user", "content": "hi"}], system="s"
    )()
    assert response.text == "hello world"
    assert (response.input_tokens, response.output_tokens) == (7, 3)
    assert captured["system"] == "s" and captured["max_tokens"] == 1024


def test_openai_completion_requests_json_mode():
    client = FakeOpenAI([make_openai_response(content='{"ok": true}')])
    assert openai_completion(client, "gpt-4o-mini")("sys", "usr") == '{"ok": true}'
    request = client.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0


def test_module_entry_point(tmp_path):
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "tracegate", "run", "Send alert email to admin"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "success"
````

#### `tests/test_rag.py`

````python
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
````


## 12. Operating notes and limits

- The budget is checked after each model call. The call that crosses the limit is still billed, and no further call is made.
- Loop detection compares exact tool names and canonical arguments. An agent that varies its arguments every time is stopped by the step limit rather than by the cycle check.
- Token counts come from the provider response. The reference agent estimates them.
- The default prices are reference values. Set a pricing file with current rates for every model you run. A model without a price fails the run on purpose.
- Input and output attributes are clipped to `max_payload_chars`. If the clip cuts through a tool's JSON arguments, the tool call is stored with the arguments as raw text.
- The harness and the evaluation runner use a synchronous interface. Concurrency in evaluation comes from a thread pool, with a fresh agent per case.
- A run is traced in the current process. The `TraceCollector` keeps spans in memory only until the run finishes and releases them when the result is built.
- Ragas is pinned below 0.5 because its interface changes between releases.
- Scorecard thresholds are policy. The defaults are a starting point and should be tuned to the dataset and the tolerance for failure in your system.

## 13. Verification record

The project was built and checked in a Linux container with Python 3.12.3.

- `ruff check` and `ruff format --check` pass on `src`, `tests` and `examples`.
- `pytest` result with Ragas installed: 85 passed. Statement coverage of the package: 97%.
- A clean, non-editable install (`pip install ".[dev,otlp]"` into a fresh virtual environment) runs `tracegate eval` with exit code 0 and passes the suite (the two Ragas tests are skipped there because Ragas is not installed).
- `tracegate eval` on the bundled dataset passes all 8 cases with verdict pass.
- OTLP export was verified against a local HTTP receiver: request path, `Authorization` header, protobuf content type and span names in the body.
- The OpenAI tool loop, the OpenAI and Anthropic adapters and the judge completion function were verified against fake clients that reproduce the response shapes.
- Ragas was verified with `ragas` 0.4.3, `langchain-community` 0.3.x and `langchain-core` 0.3.x, using an offline Ragas metric. The LLM-based Ragas metrics need an API key and were not run.
- `docker-compose.yml` and the workflow file parse as valid YAML.

Not exercised in this environment: a live Phoenix or Langfuse instance, live OpenAI or Anthropic calls, `docker build` and `docker compose up`, a run of the GitHub Actions workflow, and Python 3.10 and 3.11 (the workflow matrix covers them).

