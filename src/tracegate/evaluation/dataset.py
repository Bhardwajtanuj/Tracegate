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
