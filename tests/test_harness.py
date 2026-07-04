"""C5 — WARM/COLD benchmark harness. Tests first (TDD RED).

Invariants under test (BUILD_SPEC C5):
- a COLD run scores a task set deterministically (success_rate / mean_score)
- runs are reproducible: same seed -> identical records
- WARM minus COLD lift is computed from the two arms (the curve primitive)
- a solver crash is a failed task (a MISTAKE), never a harness crash
- the run is logged one row per task (reproducibility artifact)
"""
import json

from bench.harness import COLD, COLD_NAIVE, WARM, Task, lift, run


def _task(tid, payload, scorer):
    return Task(id=tid, payload=payload, verify=scorer)


def _binary(expected):
    return lambda answer: 1.0 if answer == expected else 0.0


def test_cold_run_scores_deterministically():
    tasks = [
        _task("t1", "a", _binary("a")),   # solver echoes -> pass
        _task("t2", "b", _binary("b")),   # pass
        _task("t3", "c", _binary("WRONG")),  # fail
    ]
    report = run(tasks, solver=lambda payload, lessons: payload, seed=0)
    assert report.arm == "cold"
    assert report.n == 3
    assert report.success_rate == 2 / 3
    assert report.mean_score == 2 / 3


def test_run_is_reproducible():
    tasks = [_task("t1", "a", _binary("a"))]
    solver = lambda payload, lessons: payload
    a = run(tasks, solver=solver, seed=42)
    b = run(tasks, solver=solver, seed=42)
    assert a.records == b.records


def test_warm_lift_over_cold():
    tasks = [_task("t1", "x", lambda answer: 1.0 if answer == "solved" else 0.0)]
    # cold solver can't solve it; warm solver solves it iff it was handed a lesson
    cold_solver = lambda payload, lessons: "stuck"
    warm_solver = lambda payload, lessons: "solved" if lessons else "stuck"

    cold = run(tasks, solver=cold_solver, seed=0)
    warm = run(tasks, solver=warm_solver, recall=lambda task: ["use the X trick"], seed=0)

    assert cold.mean_score == 0.0
    assert warm.mean_score == 1.0
    assert warm.arm == "warm"
    assert lift(warm, cold)["mean_score_lift"] == 1.0
    assert lift(warm, cold)["success_rate_lift"] == 1.0


def test_naive_arm_is_distinct_from_warm():
    """The 'never cut' three-arm bar: COLD < COLD+naive < WARM, each its own label.

    COLD+naive and WARM both pass a recall callable; only the explicit arm label
    distinguishes naive context-stuffing from real Mimir recall — so a naive win
    can't masquerade as a WARM win.
    """
    tasks = [_task("t1", "x", lambda answer: 1.0 if answer == "solved" else 0.0)]
    solver = lambda payload, lessons: "solved" if lessons else "stuck"

    naive = run(tasks, solver=solver, recall=lambda t: ["stuff everything"],
                arm=COLD_NAIVE, seed=0)
    warm = run(tasks, solver=solver, recall=lambda t: ["the X trick"],
               arm=WARM, seed=0)

    assert naive.arm == COLD_NAIVE
    assert warm.arm == WARM
    assert naive.arm != warm.arm  # naive cannot masquerade as warm


def test_arm_defaults_preserve_cold_warm_inference():
    tasks = [_task("t1", "a", _binary("a"))]
    solver = lambda payload, lessons: payload
    assert run(tasks, solver=solver, seed=0).arm == COLD
    assert run(tasks, solver=solver, recall=lambda t: [], seed=0).arm == WARM


def test_solver_crash_is_failed_task_not_harness_crash():
    def boom(payload, lessons):
        raise RuntimeError("solver blew up")

    report = run([_task("t1", "a", _binary("a"))], solver=boom, seed=0)
    assert report.records[0]["score"] == 0.0  # counted as a MISTAKE, run survived


def test_run_is_logged_one_row_per_task(tmp_path):
    log = tmp_path / "run.jsonl"
    tasks = [_task("t1", "a", _binary("a")), _task("t2", "b", _binary("b"))]
    run(tasks, solver=lambda payload, lessons: payload, seed=7, log_path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["arm"] == "cold"
    assert row["seed"] == 7
    assert row["task_id"] == "t1"
