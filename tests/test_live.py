"""Live CLI integration. Verified with a FAKE runner — no real `claude`, no tokens.

Proves: the subprocess adapter parses the CLI envelope, code is extracted from a
fence, the exec-based verifier actually runs each fix, and WARM > COLD = NAIVE
holds across all tasks through the real solver path (the gate excludes poison).
"""
import json

import pytest

from bench import live
from bench.claude_cli import ClaudeLimitError, extract_code, run_claude
from bench.harness import run
from mimir.store import InMemoryLessonStore

# Per-task (correct, trap, broken) source the fake "model" returns. correct passes
# the task's check; trap and broken both fail it — so only recalling the fix wins.
SNIPPETS = {
    "t-flush": (
        "def persist(rec):\n    store.save(rec)\n    store.commit()\n",
        "def persist(rec):\n    store.save(rec)\n    store.flush()\n",
        "def persist(rec):\n    store.save(rec)\n",
    ),
    "t-page": (
        "def all_ids():\n    out = []\n    page = 0\n    while True:\n"
        "        chunk = api.list(page)\n        if not chunk:\n            break\n"
        "        out += chunk if page == 0 else chunk[1:]\n        page += 1\n"
        "    return out\n",
        "def all_ids():\n    out = []\n    page = 0\n    while True:\n"
        "        chunk = api.list(page)\n        if not chunk:\n            break\n"
        "        out += chunk\n        page += 1\n    return out\n",
        "def all_ids():\n    return api.list()\n",
    ),
    "t-unit": (
        "def remind_in(seconds):\n    timer.schedule(seconds * 1_000_000)\n",
        "def remind_in(seconds):\n    timer.schedule(seconds * 1000)\n",
        "def remind_in(seconds):\n    timer.schedule(seconds)\n",
    ),
}


def _fake_runner_for(task):
    """A stand-in 'model': obeys the trap if present, else the fix, else stays broken."""
    correct, trap, broken = SNIPPETS[task.id]

    def runner(prompt, timeout):
        if task.trap_rule in prompt:
            body = trap
        elif task.fix_rule in prompt:
            body = correct
        else:
            body = broken
        return json.dumps({"result": f"```python\n{body}```"})  # CLI json envelope
    return runner


def test_run_claude_parses_json_envelope():
    out = run_claude("hi", _runner=lambda p, t: json.dumps({"result": "  hello "}))
    assert out == "hello"


def test_run_claude_falls_back_to_plain_text():
    assert run_claude("hi", _runner=lambda p, t: "not json") == "not json"


def test_extract_code_pulls_fenced_block():
    assert extract_code("blah\n```python\nx = 1\n```\nthanks") == "x = 1"


def test_session_limit_skips_retries_and_aborts_the_run():
    calls = []

    def limited(prompt, timeout):
        calls.append(1)
        raise ClaudeLimitError("429: session limit")

    with pytest.raises(ClaudeLimitError):
        run_claude("hi", _runner=limited)
    assert len(calls) == 1  # no pointless retries against a hard limit

    def solver(payload, lessons):
        return run_claude("hi", _runner=limited)

    with pytest.raises(ClaudeLimitError):  # harness aborts instead of scoring 0s
        run(live.TASKS, solver)


@pytest.mark.parametrize("task", live.TASKS, ids=lambda t: t.id)
def test_exec_verifier_separates_fix_from_trap_and_broken(task):
    correct, trap, broken = SNIPPETS[task.id]
    assert task.verify(correct) == 1.0   # the real fix passes the asserts
    assert task.verify(trap) == 0.0      # the tempting-but-wrong fix fails
    assert task.verify(broken) == 0.0    # the original bug fails


def test_warm_beats_cold_and_naive_through_real_solver_path():
    store = InMemoryLessonStore()
    live.seed_poison(store, live.TASKS)
    solver = lambda payload, lessons: live.cli_solver(
        payload, lessons, _runner=_fake_runner_for(payload))

    cold, naive, warm = live.run_live(store, live.TASKS, key="t", solver=solver)

    assert warm.success_rate == 1.0   # gated recall surfaces only each task's fix
    assert cold.success_rate == 0.0   # no memory -> every bug unfixed
    assert naive.success_rate == 0.0  # ungated stuffing lets the poison mislead


def test_run_live_repeated_aggregates_arms_with_band():
    def make_store():
        s = InMemoryLessonStore()
        live.seed_poison(s, live.TASKS)
        return s

    solver = lambda payload, lessons: live.cli_solver(
        payload, lessons, _runner=_fake_runner_for(payload))

    r = live.run_live_repeated(make_store, live.TASKS, key="t", repeats=3, solver=solver)

    # deterministic fake -> every repeat identical, so mean is exact and the band is tight
    assert r["warm"] == {"mean": 1.0, "min": 1.0, "max": 1.0, "n": 3}
    assert r["cold"]["mean"] == 0.0
    assert r["naive"]["mean"] == 0.0
    assert r["lift_mean"] == 1.0
