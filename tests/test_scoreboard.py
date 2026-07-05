"""Zero-token test for the scoreboard orchestrator: an injected scripted solver
reproduces COLD/NAIVE fail, WARM pass without calling the real `claude` CLI."""
import json

import pytest

from bench import scoreboard
from bench.live import TASKS

# Real fixes for the three fictional-API tasks, keyed by task id. The scripted
# solver returns these ONLY when the task's fix_rule is present and its trap_rule
# is absent — i.e. only in the WARM (gated) arm.
CORRECT = {
    "t-flush": "def persist(rec):\n    store.save(rec)\n    store.commit()\n",
    "t-page": (
        "def all_ids():\n"
        "    out = []\n"
        "    page = 0\n"
        "    while True:\n"
        "        chunk = api.list(page)\n"
        "        if not chunk:\n"
        "            break\n"
        "        out += chunk if page == 0 else chunk[1:]\n"
        "        page += 1\n"
        "    return out\n"
    ),
    "t-unit": "def remind_in(seconds):\n    timer.schedule(seconds * 1_000_000)\n",
}


def scripted_solver(payload, lessons):
    """Deterministic stand-in for the live model. WARM recall carries only the
    fix rule; NAIVE carries fix + trap; COLD carries nothing."""
    rules = {lo.rule for lo in lessons}
    if payload.fix_rule in rules and payload.trap_rule not in rules:
        return CORRECT[payload.id]
    return payload.broken


def test_build_results_shape_and_invariants():
    results = scoreboard.build_results(
        TASKS, repeats=3, seed=0, model="fake", solver=scripted_solver
    )
    assert results["model"] == "fake"
    assert results["repeats"] == 3

    arms = {a["key"]: a for a in results["arms"]}
    assert set(arms) == {"cold", "naive", "warm"}
    for a in arms.values():
        assert a["min"] <= a["success_rate"] <= a["max"]

    # The story: gated WARM beats memory-off COLD and ungated NAIVE.
    assert arms["warm"]["success_rate"] >= arms["cold"]["success_rate"]
    assert arms["warm"]["success_rate"] == 1.0
    assert arms["cold"]["success_rate"] == 0.0

    # Per-task grid: one row per task, pass counts within [0, repeats].
    assert {t["id"] for t in results["tasks"]} == {t.id for t in TASKS}
    for row in results["tasks"]:
        for key in ("cold", "naive", "warm"):
            assert 0 <= row[key] <= results["repeats"]
        assert row["warm"] == 3
        assert row["cold"] == 0

    assert results["lift"]["cold_to_warm"] == 1.0


def test_write_data_js_roundtrip(tmp_path):
    results = scoreboard.build_results(
        TASKS, repeats=1, seed=0, model="fake", solver=scripted_solver
    )
    out = tmp_path / "data.js"
    scoreboard.write_data_js(results, out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("window.RESULTS = ")
    assert text.rstrip().endswith(";")
    payload = json.loads(text[len("window.RESULTS = "):].rstrip().rstrip(";"))
    assert payload["model"] == "fake"
    assert len(payload["arms"]) == 3


def test_build_results_rejects_zero_repeats():
    with pytest.raises(ValueError):
        scoreboard.build_results(TASKS, repeats=0, seed=0, model="fake", solver=scripted_solver)
