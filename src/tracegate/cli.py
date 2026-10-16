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
