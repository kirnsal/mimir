"""C5 — WARM/COLD benchmark harness (skeleton).

The whole rubric headline is one curve: success-rate/score lift (WARM minus
COLD) across a task set. This skeleton proves the loop deterministically with a
COLD baseline; WARM is the same `run()` handed a `recall` callable (wired to the
C4 retrieval server later). The solver is pluggable so the harness stays
LLM-agnostic and the loop mechanics are testable without a live model.

Loop (BUILD_SPEC C5): run -> score (deterministic verifier) -> [later] failed
EPISODE captured (C1) -> consolidate (C2) -> next task recalls LESSONs (C4).
"""
from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Optional

from bench.claude_cli import ClaudeLimitError

# A task counts as a success at or above this score (the tunable calibration knob:
# binary pass/fail tasks use 1.0; partial-credit benches can lower it).
PASS_SCORE = 1.0

# The three canonical arms (BUILD_SPEC "never cut"). COLD+naive is the honesty bar:
# a real WARM win must beat naive context-stuffing, not just memory-off COLD.
COLD = "cold"
COLD_NAIVE = "cold+naive"
WARM = "warm"


@dataclass
class Task:
    id: str
    payload: Any                       # whatever the solver needs to attempt it
    verify: Callable[[Any], float]     # deterministic scorer, 0..1 — never an LLM


@dataclass
class Report:
    arm: str                           # "cold" | "warm"
    seed: int
    records: list[dict] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.records)

    @property
    def mean_score(self) -> float:
        return mean(r["score"] for r in self.records) if self.records else 0.0

    @property
    def success_rate(self) -> float:
        if not self.records:
            return 0.0
        return sum(1 for r in self.records if r["score"] >= PASS_SCORE) / len(self.records)


# solver(payload, lessons) -> answer ; recall(task) -> list[lessons]
Solver = Callable[[Any, list], Any]
Recall = Callable[[Task], list]


def run(
    tasks: list[Task],
    solver: Solver,
    *,
    recall: Optional[Recall] = None,
    arm: Optional[str] = None,
    seed: int = 0,
    log_path: Optional[Path] = None,
) -> Report:
    """Run a task set through the solver and score each.

    `arm` names the experiment arm (COLD / COLD_NAIVE / WARM). COLD+naive and WARM
    both pass a `recall` callable, so the label must be explicit to keep them apart;
    when omitted it falls back to COLD/WARM by recall presence (back-compat).
    """
    random.seed(seed)  # reproducibility: any solver stochasticity is pinned to the seed
    if arm is None:
        arm = WARM if recall is not None else COLD
    report = Report(arm=arm, seed=seed)

    for task in tasks:
        lessons = recall(task) if recall is not None else []
        error: Optional[str] = None
        try:
            answer = solver(task.payload, lessons)
            score = float(task.verify(answer))
        except ClaudeLimitError:
            raise  # session limit: every remaining call is doomed — abort, don't score 0s
        except Exception as exc:
            score = 0.0  # a crash is a failed task (a MISTAKE), not a harness crash
            error = repr(exc)
            # Surface it: with a live network solver a crash is usually infra (rate-limit/
            # timeout), NOT a wrong answer — silently scoring 0.0 would corrupt the headline.
            print(f"[harness] solver error on {task.id} ({arm}): {error}", file=sys.stderr)
        report.records.append({"task_id": task.id, "score": score,
                               "n_lessons": len(lessons), "error": error})

    if log_path is not None:
        _write_log(report, Path(log_path))
    return report


def ablation_credit(task: Task, lessons: list, lesson_id: str, solver: Solver,
                    *, seed: int = 0) -> float:
    """Single-lesson counterfactual ablation: WARM score with the lesson minus without it.

    The C5 headline attribution — re-run a WARM task with one LESSON removed; the
    score drop is that lesson's credit. This is the part Cognee Cloud will not ship.
    """
    full = run([task], solver, recall=lambda t: lessons, arm=WARM, seed=seed)
    kept = [lo for lo in lessons if getattr(lo, "id", None) != lesson_id]
    ablated = run([task], solver, recall=lambda t: kept, arm=WARM, seed=seed)
    return full.mean_score - ablated.mean_score


def lift(warm: Report, cold: Report) -> dict:
    """The headline curve primitive: WARM minus COLD."""
    return {
        "mean_score_lift": warm.mean_score - cold.mean_score,
        "success_rate_lift": warm.success_rate - cold.success_rate,
    }


def _write_log(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in report.records:
            fh.write(json.dumps({"arm": report.arm, "seed": report.seed, **r}, sort_keys=True) + "\n")
