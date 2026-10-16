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
