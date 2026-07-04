"""Live judge/probe wiring — verified with fake runners, ZERO tokens."""
import json

from pytest import approx

from bench.claude_judge import make_live_judge, make_solver_probe, _parse_verdict
from bench.live import CodeTask
from mimir.models import Episode


def _runner(payload):
    """A fake run_claude inner: ignores the prompt, returns `payload` in the CLI JSON envelope."""
    return lambda prompt, timeout: json.dumps({"result": payload})


def test_judge_parses_json_into_verdict():
    raw = '{"rule": "guard empty input", "specificity": 0.9, ' \
          '"generalizability": 0.8, "non_sycophancy": 0.95}'
    judge = make_live_judge(runner=_runner(raw))
    v = judge(Episode(task_id="t", context="bug"))
    assert v.rule == "guard empty input"
    assert v.passes()
    assert v.confidence == approx((0.9 + 0.8 + 0.95) / 3)


def test_judge_tolerates_prose_around_json():
    raw = 'Here is my verdict:\n{"rule": "x", "specificity": 1, ' \
          '"generalizability": 1, "non_sycophancy": 1}\nHope that helps.'
    v = make_live_judge(runner=_runner(raw))(Episode())
    assert v.rule == "x" and v.passes()


def test_malformed_response_fails_closed():
    # Garbage, empty rule, and out-of-contract all must NOT admit a lesson.
    for bad in ["not json at all", '{"rule": ""}', '{"specificity": 0.9}']:
        v = make_live_judge(runner=_runner(bad))(Episode())
        assert not v.passes()


def test_scores_clamp_into_range():
    raw = '{"rule": "x", "specificity": 5, "generalizability": -2, "non_sycophancy": 0.5}'
    v = _parse_verdict(raw)
    assert v.specificity == 1.0 and v.generalizability == 0.0


def test_solver_probe_measures_real_pass_rate():
    task = CodeTask(id="p", prompt="add two numbers", broken="",
                    check="assert add(2, 3) == 5", fix_rule="", trap_rule="")
    good = lambda payload, lessons: "def add(a, b):\n    return a + b\n"
    bad = lambda payload, lessons: "def add(a, b):\n    return 0\n"
    assert make_solver_probe([task], good)([]) == 1.0
    assert make_solver_probe([task], bad)([]) == 0.0
    assert make_solver_probe([], good)([]) == 0.0  # empty held-out -> 0, never crashes
