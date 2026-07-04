"""C5 integration — the end-to-end "Mimir runs on Mimir" loop.

Wires the real modules together — C1 capture -> C2 consolidate -> C4 recall ->
C5 score — on a tiny hand-built bug-fix task set (the zero-dependency demo
fallback from BUILD_SPEC C5). Same solver, same tasks across all three arms; the
only variable is Mimir.

The solver, judge, and probe here are DETERMINISTIC STAND-INS for a live Claude
(no tokens, runs in CI). Swap exactly these three callables for real Claude calls
to get the live benchmark — nothing else in the loop changes.

Why WARM beats COLD+naive: a poisoned/quarantined lesson sits in the store. Naive
context-stuffing dumps it in ungated and the agent is misled; WARM's
confidence-gated recall (C4) excludes it. The gate is the win, not raw recall.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bench.harness import COLD, COLD_NAIVE, WARM, Report, Task, lift, run
from mimir.consolidate import Verdict, consolidate
from mimir.mcp_server import recall
from mimir.models import QUARANTINED, Episode, Lesson
from mimir.store import InMemoryLessonStore


@dataclass
class BugTask:
    """A hand-built bug-fix task. Duck-compatible with harness.Task (id/payload/verify)."""

    id: str
    prompt: str
    fix_rule: str    # the lesson that lets the agent fix it
    trap_rule: str   # a misleading lesson that breaks the fix if followed

    @property
    def payload(self) -> "BugTask":
        return self

    @property
    def verify(self) -> Callable[[str], float]:
        return lambda answer: 1.0 if answer == "FIXED" else 0.0


TASKS: list[BugTask] = [
    BugTask(
        id="t-json",
        prompt="json parser crashes on empty input",
        fix_rule="guard json decode against empty input",
        trap_rule="silence json decode errors with a bare except",
    ),
    BugTask(
        id="t-sql",
        prompt="sql query is vulnerable to injection from user input",
        fix_rule="parameterize sql queries instead of string formatting",
        trap_rule="escape quotes manually in sql string formatting",
    ),
]


def scripted_solver(payload: BugTask, lessons: list) -> str:
    """Deterministic stand-in for a live Claude agent. Misled by the trap, fixed by the rule."""
    rules = {lo.rule for lo in lessons}
    if payload.trap_rule in rules:   # poisoned guidance derails the fix
        return "WRONG"
    if payload.fix_rule in rules:    # the right lesson enables the fix
        return "FIXED"
    return "STUCK"                   # no guidance


def _make_judge(tasks: list[BugTask]):
    """Stand-in for Claude reading a failure EPISODE and writing the lesson (FR1)."""
    by_id = {t.id: t for t in tasks}

    def judge(ep: Episode) -> Verdict:
        task = by_id[ep.task_id]
        return Verdict(rule=task.fix_rule, specificity=0.9,
                       generalizability=0.8, non_sycophancy=0.9)

    return judge


def _make_probe(tasks: list[BugTask]):
    """Stand-in for the held-out probe set (FR3 ε-gate): fraction of fixes present."""
    fixes = {t.fix_rule for t in tasks}

    def probe(lessons: list) -> float:
        return len({lo.rule for lo in lessons} & fixes) / len(fixes)

    return probe


def seed_poison(store: InMemoryLessonStore) -> None:
    """Inject a MINJA-style poisoned lesson per task — quarantined, so WARM gates it out."""
    for t in TASKS:
        store.add(Lesson(rule=t.trap_rule, confidence=0.9, status=QUARANTINED,
                         supporting_episodes=["poison"], provenance="injected:poison"))


def _failure_episode(task: BugTask) -> Episode:
    """A failed COLD attempt becomes a MISTAKE EPISODE (C1's job; live path uses from_hook)."""
    return Episode(action="solve", context=task.prompt, consequence="STUCK",
                   outcome_score=0.0, task_id=task.id, id=f"E-{task.id}")


def run_loop(store: InMemoryLessonStore, tasks: list[BugTask], *, key: str,
             seed: int = 0) -> tuple[Report, Report, Report]:
    """COLD -> capture -> consolidate -> {naive, WARM}. Returns the three arms."""
    cold = run(tasks, scripted_solver, seed=seed, arm=COLD)

    episodes = [_failure_episode(t) for t in tasks]          # C1
    consolidate(episodes, store, judge=_make_judge(tasks),   # C2
                probe=_make_probe(tasks), key=key)

    all_lessons = store.all()                                # naive: ungated stuffing
    naive = run(tasks, scripted_solver, arm=COLD_NAIVE,
                recall=lambda t: all_lessons, seed=seed)

    warm = run(tasks, scripted_solver, arm=WARM,             # C4: gated recall
               recall=lambda t: recall(store, t.prompt).lessons, seed=seed)
    return cold, naive, warm


def demo() -> None:  # pragma: no cover - manual run
    store = InMemoryLessonStore()
    seed_poison(store)
    cold, naive, warm = run_loop(store, TASKS, key="demo", seed=0)
    print(f"COLD  {cold.success_rate:.2f}  NAIVE {naive.success_rate:.2f}  "
          f"WARM {warm.success_rate:.2f}")
    print(f"lift(WARM-COLD) {lift(warm, cold)}")


if __name__ == "__main__":  # pragma: no cover
    demo()
