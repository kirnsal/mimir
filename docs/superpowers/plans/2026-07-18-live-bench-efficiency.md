# Live Bench Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `bench/live.py`'s live WARM/COLD benchmark run tasks concurrently (same token cost, less wall-clock time) and print the cost/latency metrics `bench/harness.py` already computes but never surfaces.

**Architecture:** `bench/harness.py`'s `run()` gains an opt-in `max_workers` parameter — `None`/`1` keeps today's exact sequential loop, `>1` dispatches each task's solver call to a `ThreadPoolExecutor` (real concurrency since `subprocess.run`-backed solvers release the GIL while blocked on I/O), reassembling results in original task order. `bench/live.py`'s `run_live`/`run_live_repeated` forward this parameter through; `demo()`/`demo_band()` read a `MIMIR_BENCH_WORKERS` env var and print `net_value()`'s cost/latency numbers alongside the existing success-rate/lift output.

**Tech Stack:** Python stdlib only (`concurrent.futures.ThreadPoolExecutor`) — no new dependency.

## Global Constraints

- `max_workers: Optional[int] = None` on `harness.run()`. `None` or any value `<= 1` is the exact pre-existing sequential codepath — every current caller and test must pass unmodified with zero behavior change.
- Concurrent mode (`max_workers > 1`) reassembles `report.records` in the original `tasks` order, not completion order.
- `duration_s` is measured per-task around each individual solver call in both modes.
- On `ClaudeLimitError` from any task, cancel every not-yet-started future and re-raise once the in-flight batch settles — do not silently swallow it, do not score the remaining tasks. Already-completed results in that window are discarded (the whole run is aborting).
- No new CLI argument parser in `bench/live.py`. Config goes through a `MIMIR_BENCH_WORKERS` env var (default `3` when unset; `1` restores sequential), consistent with how `MIMIR_CLAUDE_BIN` already works in `bench/claude_cli.py`.
- `run_live`/`run_live_repeated` keep `max_workers: Optional[int] = None` as their own default (library-level, unaffected import-time behavior) — only `demo()`/`demo_band()` (the CLI entry points) read the env var and pass it through.
- All new behavior must be testable at zero token cost via the existing injectable-solver/fake-runner pattern already used in `tests/test_harness.py` and `tests/test_live.py`. `demo()`/`demo_band()` themselves stay untested (real token spend, `# pragma: no cover`, unchanged from today).
- Out of scope, do not touch: the number of live calls a benchmark run makes (COLD/NAIVE/WARM each need their own call, no caching); `bench/loop.py`'s deterministic free demo path (unaffected since `max_workers` defaults to `None`); `claude_cli.py`'s retry/backoff logic.

---

### Task 1: Opt-in concurrency in `bench/harness.py`

**Files:**
- Modify: `bench/harness.py` — imports (lines 12-23), insert `_run_one_task` helper before `run()` (before line 95), replace `run()` (lines 95-150)
- Test: `tests/test_harness.py` — add `ClaudeLimitError` and `threading`/`time` imports, append new tests at end of file (after line 233)

**Interfaces:**
- Consumes: nothing new — `Task`, `Report`, `_apply_budget`, `ClaudeLimitError` already exist in this file/its imports.
- Produces: `run(..., max_workers: Optional[int] = None) -> Report` — Task 2 in `bench/live.py` calls this with an explicit `max_workers` value.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_harness.py`. First add these two imports near the top of the file (after the existing `import json` on line 10, before the blank line and `from bench.harness import ...` on line 12):

```python
import json
import threading
import time

from bench.claude_cli import ClaudeLimitError
from bench.harness import COLD, COLD_NAIVE, WARM, Task, lift, net_value, run
```

(This replaces the current lines 10-12, which are `import json`, a blank line, and `from bench.harness import COLD, COLD_NAIVE, WARM, Task, lift, net_value, run`.)

Then append at the end of the file (after `test_run_is_logged_one_row_per_task`, line 233):

```python
# --- Opt-in concurrency: max_workers dispatches solver calls to a thread pool ------

def test_max_workers_one_matches_default_sequential_records():
    tasks = [_task("t1", "a", _binary("a")), _task("t2", "b", _binary("b"))]
    solver = lambda payload, lessons: payload
    default = run(tasks, solver=solver, seed=0, _clock=lambda: 0.0)
    explicit_one = run(tasks, solver=solver, seed=0, max_workers=1, _clock=lambda: 0.0)
    assert default.records == explicit_one.records


def test_concurrent_run_executes_tasks_in_parallel():
    tasks = [_task(f"t{i}", i, lambda answer: 1.0) for i in range(3)]
    active = []
    max_concurrent = []
    lock = threading.Lock()

    def solver(payload, lessons):
        with lock:
            active.append(1)
            max_concurrent.append(len(active))
        time.sleep(0.05)
        with lock:
            active.pop()
        return payload

    run(tasks, solver=solver, seed=0, max_workers=3)
    assert max(max_concurrent) > 1  # at least two solver calls genuinely overlapped


def test_concurrent_run_preserves_task_order_despite_completion_order():
    tasks = [_task("slow", "a", _binary("a")), _task("fast", "b", _binary("b"))]

    def solver(payload, lessons):
        if payload == "a":
            time.sleep(0.05)  # slow task finishes after the fast one
        return payload

    report = run(tasks, solver=solver, seed=0, max_workers=2)
    assert [r["task_id"] for r in report.records] == ["slow", "fast"]


def test_concurrent_run_raises_claude_limit_error():
    tasks = [_task("t1", "a", _binary("a")), _task("t2", "b", _binary("b"))]

    def solver(payload, lessons):
        raise ClaudeLimitError("429: session limit")

    with pytest.raises(ClaudeLimitError):
        run(tasks, solver=solver, seed=0, max_workers=2)
```

`test_concurrent_run_raises_claude_limit_error` needs `pytest` imported — add `import pytest` to the same import block from Step 1 above (it is not currently imported in `tests/test_harness.py`).

Full corrected top-of-file import block:

```python
import json
import threading
import time

import pytest

from bench.claude_cli import ClaudeLimitError
from bench.harness import COLD, COLD_NAIVE, WARM, Task, lift, net_value, run
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_harness.py -v -k "max_workers or concurrent"`
Expected: FAIL — `TypeError: run() got an unexpected keyword argument 'max_workers'`

- [ ] **Step 3: Write the minimal implementation**

In `bench/harness.py`, update the imports (lines 12-23):

```python
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Optional

from bench.claude_cli import ClaudeLimitError
```

Insert `_run_one_task` immediately before `def run(` (before line 95):

```python
def _run_one_task(task: Task, solver: Solver, recall: Optional[Recall], budget: Optional[int],
                  arm: str, _clock: Callable[[], float]) -> dict:
    """One task's solver call -> its report record. Shared by the sequential and
    concurrent paths in run() so both behave identically per task."""
    lessons = _apply_budget(recall(task) if recall is not None else [], budget)
    error: Optional[str] = None
    start = _clock()
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
    duration = _clock() - start
    return {"task_id": task.id, "score": score,
           "n_lessons": len(lessons),
           "lesson_ids": [getattr(lo, "id", None) for lo in lessons],
           "duration_s": duration,
           "n_lesson_chars": sum(len(getattr(lo, "rule", "")) for lo in lessons),
           "error": error}
```

Replace `run()` (lines 95-150) with:

```python
def run(
    tasks: list[Task],
    solver: Solver,
    *,
    recall: Optional[Recall] = None,
    arm: Optional[str] = None,
    seed: int = 0,
    log_path: Optional[Path] = None,
    budget: Optional[int] = None,
    max_workers: Optional[int] = None,
    _clock: Callable[[], float] = time.perf_counter,
) -> Report:
    """Run a task set through the solver and score each.

    `arm` names the experiment arm (COLD / COLD_NAIVE / WARM). COLD+naive and WARM
    both pass a `recall` callable, so the label must be explicit to keep them apart;
    when omitted it falls back to COLD/WARM by recall presence (back-compat).

    `budget` (chars of recalled-lesson text, C5 equal-budget control) applies
    identically to every arm passed the same value — the honesty bar that stops a
    naive context-stuffing arm from winning purely on volume.

    `max_workers` (None or 1, the default) runs tasks sequentially, unchanged from
    every prior release. A value > 1 dispatches solver calls to a thread pool — real
    wall-clock concurrency for I/O-bound solvers (e.g. a subprocess-backed live model
    call) since Python releases the GIL while blocked on I/O. Records are always
    reassembled in `tasks` order regardless of completion order, so logs stay
    reproducible. A ClaudeLimitError from any task still aborts the whole run
    (cancelling not-yet-started work) rather than scoring the rest — same contract
    sequential or concurrent.

    Each record also carries `duration_s` (wall time around the solver call) and
    `n_lesson_chars` (recalled-lesson text volume, post-budget) — the C5 net-value
    cost signal (`net_value()` below). `_clock` is injectable for deterministic tests.
    """
    random.seed(seed)  # reproducibility: any solver stochasticity is pinned to the seed
    if arm is None:
        arm = WARM if recall is not None else COLD
    report = Report(arm=arm, seed=seed)

    if not max_workers or max_workers <= 1:
        for task in tasks:
            report.records.append(_run_one_task(task, solver, recall, budget, arm, _clock))
    else:
        records: list[Optional[dict]] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_one_task, task, solver, recall, budget, arm, _clock): i
                      for i, task in enumerate(tasks)}
            try:
                for future in as_completed(futures):
                    records[futures[future]] = future.result()
            except ClaudeLimitError:
                for f in futures:
                    f.cancel()
                raise
        report.records = records

    if log_path is not None:
        _write_log(report, Path(log_path))
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_harness.py -v`
Expected: all tests PASS, including the 4 new ones. (`test_concurrent_run_executes_tasks_in_parallel` and `test_concurrent_run_preserves_task_order_despite_completion_order` use real `time.sleep` and are not injected via `_clock` — they assert genuine thread overlap / order, not exact timing values, so they should not be flaky, but re-run once if either fails to rule out scheduler noise before treating it as a real bug.)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest`
Expected: all tests PASS (baseline was 171 passed, 2 deselected; expect 171 + 4 = 175 passed, 2 deselected).

- [ ] **Step 6: Commit**

```bash
git add bench/harness.py tests/test_harness.py
git commit -m "feat: add opt-in concurrency to harness.run() via max_workers"
```

---

### Task 2: Wire `bench/live.py` — concurrency passthrough + cost/latency reporting

**Files:**
- Modify: `bench/live.py` — imports (lines 14-24), insert `_worker_count` helper before `demo()` (before line 269), modify `run_live` (lines 179-205), `run_live_repeated` (lines 212-232), `demo` (lines 269-278), `demo_band` (lines 281-296)
- Test: `tests/test_live.py` — append new tests at end of file (after line 125)

**Interfaces:**
- Consumes: `run(..., max_workers: Optional[int] = None) -> Report` from Task 1 (`bench.harness`).
- Produces: `run_live(..., max_workers: Optional[int] = None)`, `run_live_repeated(..., max_workers: Optional[int] = None)` — no other task consumes these; this is the plan's terminal, user-facing deliverable. `_worker_count() -> int` is internal to `bench/live.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live.py` (after `test_run_live_repeated_aggregates_arms_with_band`, line 125):

```python
# --- MIMIR_BENCH_WORKERS -> concurrency knob for demo()/demo_band() ----------------

def test_worker_count_defaults_to_three_when_env_unset(monkeypatch):
    monkeypatch.delenv("MIMIR_BENCH_WORKERS", raising=False)
    assert live._worker_count() == 3


def test_worker_count_reads_env_var(monkeypatch):
    monkeypatch.setenv("MIMIR_BENCH_WORKERS", "5")
    assert live._worker_count() == 5


def test_worker_count_one_restores_sequential_default(monkeypatch):
    monkeypatch.setenv("MIMIR_BENCH_WORKERS", "1")
    assert live._worker_count() == 1


# --- max_workers passthrough: run_live / run_live_repeated forward it to run() -----

def test_run_live_forwards_max_workers_to_run(monkeypatch):
    calls = []
    real_run = live.run

    def spy_run(*args, **kwargs):
        calls.append(kwargs.get("max_workers"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(live, "run", spy_run)
    store = InMemoryLessonStore()
    live.seed_poison(store, live.TASKS)
    solver = lambda payload, lessons: live.cli_solver(
        payload, lessons, _runner=_fake_runner_for(payload))

    live.run_live(store, live.TASKS, key="t", solver=solver, max_workers=5)

    assert calls == [5, 5, 5]  # cold, naive, warm each forwarded max_workers


def test_run_live_repeated_forwards_max_workers(monkeypatch):
    calls = []
    real_run = live.run

    def spy_run(*args, **kwargs):
        calls.append(kwargs.get("max_workers"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(live, "run", spy_run)

    def make_store():
        s = InMemoryLessonStore()
        live.seed_poison(s, live.TASKS)
        return s

    solver = lambda payload, lessons: live.cli_solver(
        payload, lessons, _runner=_fake_runner_for(payload))
    live.run_live_repeated(make_store, live.TASKS, key="t", repeats=2, solver=solver,
                           max_workers=2)

    assert calls == [2] * 6  # 3 arms x 2 repeats


# --- cost/latency metrics surfaced from run_live_repeated --------------------------

def test_run_live_repeated_reports_added_latency_mean():
    def make_store():
        s = InMemoryLessonStore()
        live.seed_poison(s, live.TASKS)
        return s

    solver = lambda payload, lessons: live.cli_solver(
        payload, lessons, _runner=_fake_runner_for(payload))
    r = live.run_live_repeated(make_store, live.TASKS, key="t", repeats=2, solver=solver)

    assert "added_latency_mean_s" in r
    assert isinstance(r["added_latency_mean_s"], float)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v -k "worker_count or forwards_max_workers or added_latency_mean"`
Expected: FAIL — `AttributeError: module 'bench.live' has no attribute '_worker_count'` (and `TypeError: run_live() got an unexpected keyword argument 'max_workers'` for the forwarding tests, and `KeyError: 'added_latency_mean_s'` for the last one, once the earlier failures are fixed one at a time — implement Step 3 in full before re-running, all three classes of failure share one root cause: the new surface doesn't exist yet).

- [ ] **Step 3: Write the minimal implementation**

In `bench/live.py`, update the imports (lines 14-24):

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from statistics import mean
from typing import Callable, Optional

from bench.claude_cli import extract_code, run_claude
from bench.harness import COLD, COLD_NAIVE, WARM, Report, lift, net_value, run
from mimir.mcp_server import recall
from mimir.models import QUARANTINED, Lesson
from mimir.store import InMemoryLessonStore
```

Replace `run_live` (lines 179-205):

```python
def run_live(store: InMemoryLessonStore, tasks: list[CodeTask], *, key: str,
             solver: Optional[Callable] = None, judge: Optional[Callable] = None,
             probe: Optional[Callable] = None, seed: int = 0,
             max_workers: Optional[int] = None,
             ) -> tuple[Report, Report, Report]:
    """COLD -> capture -> consolidate -> {naive, WARM} with a live (or injected) solver.

    judge/probe default to the deterministic stand-ins (token-free); pass
    make_live_judge()/make_solver_probe() for fully-live consolidation.

    `max_workers` forwards to every harness.run() call below — None (default) is
    today's exact sequential behavior; see harness.run()'s docstring.
    """
    from mimir.consolidate import consolidate
    from mimir.models import Episode

    solve = solver or cli_solver
    cold = run(tasks, solve, seed=seed, arm=COLD, max_workers=max_workers)

    # Enrich the failure EPISODE with the broken source so a live judge has real material.
    episodes = [Episode(action="attempted fix", context=t.prompt,
                        consequence=f"failed; broken code:\n{t.broken}",
                        outcome_score=0.0, task_id=t.id, id=f"E-{t.id}") for t in tasks]
    consolidate(episodes, store, judge=judge or _make_judge(tasks),
                probe=probe or _make_probe(tasks), key=key)

    all_lessons = store.all()
    naive = run(tasks, solve, arm=COLD_NAIVE, recall=lambda t: all_lessons, seed=seed,
               max_workers=max_workers)
    warm = run(tasks, solve, arm=WARM,
               recall=lambda t: recall(store, t.prompt).lessons, seed=seed,
               max_workers=max_workers)
    return cold, naive, warm
```

Replace `run_live_repeated` (lines 212-232):

```python
def run_live_repeated(make_store: Callable[[], InMemoryLessonStore], tasks: list[CodeTask],
                      *, key: str, repeats: int = 3, solver: Optional[Callable] = None,
                      judge: Optional[Callable] = None, probe: Optional[Callable] = None,
                      max_workers: Optional[int] = None,
                      ) -> dict:
    """Run the COLD/NAIVE/WARM loop `repeats` times on a FRESH store each pass; aggregate.

    A single run is a demo, not a measurement: at n=3 one task flip swings a run by 0.33
    and live Claude is stochastic (COLD seen at both 0.67 and 1.00). Repeating draws K
    independent model samples (the seed pins Python RNG, not the model) and reports each
    arm's mean + (min,max) band — the band IS the honesty: a lift smaller than the band
    is noise. `make_store` rebuilds the poisoned store per pass (consolidate mutates it).

    `added_latency_mean_s` is the C5 net-value latency signal (WARM's extra wall-clock
    over COLD, averaged across repeats) — the cost half of the lift/cost tradeoff.
    """
    cold_s, naive_s, warm_s, latency_s = [], [], [], []
    for _ in range(repeats):
        cold, naive, warm = run_live(make_store(), tasks, key=key,
                                     solver=solver, judge=judge, probe=probe,
                                     max_workers=max_workers)
        cold_s.append(cold.success_rate)
        naive_s.append(naive.success_rate)
        warm_s.append(warm.success_rate)
        latency_s.append(warm.mean_duration_s - cold.mean_duration_s)
    return {"cold": _band(cold_s), "naive": _band(naive_s), "warm": _band(warm_s),
            "lift_mean": mean(warm_s) - mean(cold_s),
            "added_latency_mean_s": mean(latency_s)}
```

Insert `_worker_count` immediately before `def demo(` (before line 269):

```python
def _worker_count(default: int = 3) -> int:
    """MIMIR_BENCH_WORKERS -> int, defaulting to `default` when unset. The claude CLI
    runs against your own subscription session, so concurrent calls hit a session-level
    rate limit (429 / ClaudeLimitError) faster than sequential ones — 3 is a
    conservative starting point, not a measured optimum. Set to 1 for today's exact
    sequential behavior."""
    raw = os.environ.get("MIMIR_BENCH_WORKERS")
    return int(raw) if raw else default
```

Replace `demo()` (lines 269-278):

```python
def demo() -> None:  # pragma: no cover - real CLI calls, spends tokens
    # Live SOLVER, but the curated (deterministic) judge supplies the lesson: on these
    # novel-API tasks an equally-ignorant live judge can't distill the contract from a
    # bare failure, so the lift headline isolates RETRIEVAL value (does carrying the
    # lesson help?) from lesson-GENERATION quality (the live judge, tested separately).
    store = InMemoryLessonStore()
    seed_poison(store, TASKS)
    cold, naive, warm = run_live(store, TASKS, key="live", max_workers=_worker_count())
    nv = net_value(warm, cold)
    print(f"COLD {cold.success_rate:.2f}  NAIVE {naive.success_rate:.2f}  "
          f"WARM {warm.success_rate:.2f}  lift {lift(warm, cold)}")
    print(f"COLD mean_duration_s {cold.mean_duration_s:.2f}  "
          f"WARM mean_duration_s {warm.mean_duration_s:.2f}  "
          f"added_latency_s {nv['added_latency_s']:+.2f}  net_value {nv['net_value']:+.2f}")
```

Replace `demo_band()` (lines 281-296):

```python
def demo_band(repeats: int = 3) -> None:  # pragma: no cover - real CLI calls, spends ~3x demo()
    """De-noised headline: repeat the loop and print each arm's mean + noise band.

    `python -c "from bench.live import demo_band; demo_band(3)"` — costs ~repeats x demo().
    A lift_mean smaller than the COLD band's (max-min) is noise, not a result.
    """
    def make_store() -> InMemoryLessonStore:
        s = InMemoryLessonStore()
        seed_poison(s, TASKS)
        return s

    r = run_live_repeated(make_store, TASKS, key="live", repeats=repeats,
                          max_workers=_worker_count())
    for arm in ("cold", "naive", "warm"):
        b = r[arm]
        print(f"{arm.upper():5} mean {b['mean']:.2f}  band [{b['min']:.2f}, {b['max']:.2f}]  (n={b['n']})")
    print(f"lift_mean (WARM-COLD) {r['lift_mean']:+.2f}")
    print(f"added_latency_mean_s (WARM-COLD) {r['added_latency_mean_s']:+.2f}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v`
Expected: all tests PASS, including the 6 new ones, with no regressions in `test_warm_beats_cold_and_naive_through_real_solver_path` or `test_run_live_repeated_aggregates_arms_with_band`.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest`
Expected: all tests PASS (baseline after Task 1 was 175 passed, 2 deselected; expect 175 + 6 = 181 passed, 2 deselected).

- [ ] **Step 6: Commit**

```bash
git add bench/live.py tests/test_live.py
git commit -m "feat: wire max_workers concurrency and cost/latency reporting into bench/live.py"
```
