# Mimir Scoreboard Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a video-ready visual scoreboard that runs Mimir's existing 3-arm (COLD/NAIVE/WARM) benchmark live against Sonnet 5 and renders the result as a self-contained HTML page.

**Architecture:** A thin presentation/wiring layer over the existing `bench/` harness. One backwards-compatible edit pins the model via an env var; a new orchestrator module runs the arms and emits a `data.js` results file; a static HTML page reads that file and draws animated bars plus a per-task pass/fail grid. The existing harness, recall gate, verifier, and solver are unchanged.

**Tech Stack:** Python ≥ 3.10 (stdlib only — no new runtime deps), the `claude` CLI as the live model, vanilla HTML/CSS/JS (no libraries, no server), pytest.

## Global Constraints

- **Zero new runtime dependencies.** The repo's core is dependency-free; do not add any. Tests use `pytest` (already a dev extra).
- **Do not modify existing harness logic.** The only edit to existing code is model-pinning in `bench/claude_cli.py`. `bench/harness.py`, `bench/live.py`, and `mimir/` are otherwise untouched.
- **The HTML page must be fully self-contained** — no external scripts, fonts, or stylesheets fetched over the network; no build step; opens by double-click via a `<script src="data.js">` include.
- **`bench/scoreboard/` must NOT contain an `__init__.py`.** It holds only static assets (`index.html`, `data.js`). This lets `python -m bench.scoreboard` resolve to the module `bench/scoreboard.py`, not the directory.
- **Results values are fractions in `[0.0, 1.0]`;** the HTML formats them as percentages.
- **A task passes when `score >= PASS_SCORE` (1.0)** — reuse `bench.harness.PASS_SCORE`, do not hardcode.
- **The demo run is invoked with `MIMIR_CLAUDE_MODEL=sonnet`** so every solver call goes to Sonnet 5.

---

## File Structure

- `bench/claude_cli.py` — **modify** `_subprocess_runner` to append `--model` when `MIMIR_CLAUDE_MODEL` is set.
- `bench/scoreboard.py` — **new** orchestrator: `build_results(...)`, `write_data_js(...)`, `main(...)`.
- `bench/scoreboard/index.html` — **new** self-contained scoreboard page.
- `bench/scoreboard/data.js` — **new** committed sample results (also the generated-output location).
- `tests/test_claude_cli_model.py` — **new** zero-token test for model pinning.
- `tests/test_scoreboard.py` — **new** zero-token test for the orchestrator (injected fake solver).

---

## Task 1: Pin the model in the CLI adapter

**Files:**
- Modify: `bench/claude_cli.py:35-52` (`_subprocess_runner`)
- Test: `tests/test_claude_cli_model.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: behavior — when env var `MIMIR_CLAUDE_MODEL` is set and non-empty, `_subprocess_runner` invokes `claude` with `--model <value>`; when unset/empty, the argument list is exactly as today (`[bin, "-p", "--output-format", "json"]`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_cli_model.py`:

```python
"""Zero-token test: MIMIR_CLAUDE_MODEL adds `--model` to the claude invocation."""
from bench import claude_cli


class _FakeProc:
    returncode = 0
    stdout = '{"result": "ok"}'
    stderr = ""


def _capture_run(monkeypatch):
    """Patch subprocess.run to record the args list and return a success envelope."""
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        return _FakeProc()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    return calls


def test_model_env_adds_flag(monkeypatch):
    calls = _capture_run(monkeypatch)
    monkeypatch.setenv("MIMIR_CLAUDE_MODEL", "sonnet")
    out = claude_cli._subprocess_runner("fix this", 10)
    assert out == '{"result": "ok"}'
    assert "--model" in calls["args"]
    assert calls["args"][calls["args"].index("--model") + 1] == "sonnet"


def test_no_model_env_keeps_args_unchanged(monkeypatch):
    calls = _capture_run(monkeypatch)
    monkeypatch.delenv("MIMIR_CLAUDE_MODEL", raising=False)
    claude_cli._subprocess_runner("fix this", 10)
    assert "--model" not in calls["args"]
    assert calls["args"][1:] == ["-p", "--output-format", "json"]


def test_empty_model_env_keeps_args_unchanged(monkeypatch):
    calls = _capture_run(monkeypatch)
    monkeypatch.setenv("MIMIR_CLAUDE_MODEL", "")
    claude_cli._subprocess_runner("fix this", 10)
    assert "--model" not in calls["args"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && python -m pytest tests/test_claude_cli_model.py -v`
Expected: `test_model_env_adds_flag` FAILS (no `--model` in args, current code never adds it).

- [ ] **Step 3: Modify `_subprocess_runner`**

In `bench/claude_cli.py`, replace the body of `_subprocess_runner` (currently lines 35-52) with:

```python
def _subprocess_runner(prompt: str, timeout: int) -> str:
    """Call the real CLI. Raises on failure with the CLI's own message (loud AND legible)."""
    args = [_resolve_bin(), "-p", "--output-format", "json"]
    model = os.environ.get("MIMIR_CLAUDE_MODEL")
    if model:                                  # pin the solver model for a benchmark (e.g. sonnet)
        args += ["--model", model]
    proc = subprocess.run(
        args,
        input=prompt, capture_output=True, text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        # the CLI reports errors (429 rate limit, session limit) inside the JSON envelope
        msg, status = proc.stderr or proc.stdout, None
        try:
            env = json.loads(proc.stdout)
            msg, status = env.get("result", msg), env.get("api_error_status")
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        cls = ClaudeLimitError if status == 429 else RuntimeError
        raise cls(f"claude exited {proc.returncode}: {str(msg)[:200]}")
    return proc.stdout
```

(`os` is already imported at the top of the file — no new import needed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && python -m pytest tests/test_claude_cli_model.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Verify nothing else broke**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && python -m pytest -q`
Expected: the full suite passes (live tests that need real `claude` may be skipped/deselected as before — no new failures).

- [ ] **Step 6: Commit**

```bash
git add bench/claude_cli.py tests/test_claude_cli_model.py
git commit -m "feat(bench): pin solver model via MIMIR_CLAUDE_MODEL"
```

---

## Task 2: Scoreboard orchestrator (`bench/scoreboard.py`)

**Files:**
- Create: `bench/scoreboard.py`
- Test: `tests/test_scoreboard.py`

**Interfaces:**
- Consumes: `bench.live.TASKS`, `bench.live.run_live(store, tasks, *, key, solver=None, judge=None, probe=None, seed=0) -> (Report, Report, Report)`, `bench.live.seed_poison(store, tasks)`, `bench.harness.PASS_SCORE`, `mimir.store.InMemoryLessonStore`. Each `Report` has `.records` (list of `{"task_id", "score", "n_lessons", "error"}`) and `.success_rate`.
- Produces:
  - `build_results(tasks, *, repeats=3, seed=0, model="default", solver=None) -> dict` returning the `data.js` payload shape (documented below).
  - `write_data_js(results: dict, out_path) -> None` writing `window.RESULTS = <json>;` to `out_path`.
  - `main(argv=None) -> None` — CLI entry (`python -m bench.scoreboard`).

Output dict shape (the contract with Task 3):

```python
{
  "model": "sonnet", "repeats": 3, "seed": 0,
  "arms": [
    {"key": "cold",  "label": "COLD (no memory)",    "success_rate": 0.0, "min": 0.0, "max": 0.0},
    {"key": "naive", "label": "NAIVE (ungated)",     "success_rate": 0.11, "min": 0.0, "max": 0.33},
    {"key": "warm",  "label": "WARM (Mimir, gated)", "success_rate": 1.0, "min": 1.0, "max": 1.0},
  ],
  "tasks": [
    {"id": "t-flush", "cold": 0, "naive": 0, "warm": 3},  # values = pass count out of `repeats`
  ],
  "lift": {"cold_to_warm": 1.0, "naive_to_warm": 0.89},
}
```

- [ ] **Step 1: Write the failing test**

Create `tests/test_scoreboard.py`:

```python
"""Zero-token test for the scoreboard orchestrator: an injected scripted solver
reproduces COLD/NAIVE fail, WARM pass without calling the real `claude` CLI."""
import json

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && python -m pytest tests/test_scoreboard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.scoreboard'`.

- [ ] **Step 3: Write the orchestrator**

Create `bench/scoreboard.py`:

```python
"""Scoreboard orchestrator — run the COLD/NAIVE/WARM arms live and emit data.js.

Reuses bench.live.run_live per repeat (not run_live_repeated, which discards each
Report.records and so can't feed the per-task grid). Aggregates per-arm success
rate bands and per-task pass counts, then writes a `window.RESULTS = {...}` file
that bench/scoreboard/index.html renders. The live model is the default solver;
inject `solver=` for zero-token tests.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import mean
from typing import Callable, Optional

from bench.harness import PASS_SCORE
from bench.live import TASKS, run_live, seed_poison
from mimir.store import InMemoryLessonStore

# Harness arm label -> data.js key. run_live returns (cold, naive, warm) in order,
# so we map positionally; these labels are what the HTML shows.
ARM_KEYS = ("cold", "naive", "warm")
ARM_LABELS = {
    "cold": "COLD (no memory)",
    "naive": "NAIVE (ungated)",
    "warm": "WARM (Mimir, gated)",
}

DEFAULT_OUT = "bench/scoreboard/data.js"


def _passed(report, task_id: str) -> bool:
    rec = next(r for r in report.records if r["task_id"] == task_id)
    return rec["score"] >= PASS_SCORE


def build_results(tasks, *, repeats: int = 3, seed: int = 0, model: str = "default",
                  solver: Optional[Callable] = None) -> dict:
    """Run the three arms `repeats` times on a fresh poisoned store each pass and
    aggregate into the data.js payload."""
    rates = {k: [] for k in ARM_KEYS}
    passes = {t.id: {k: 0 for k in ARM_KEYS} for t in tasks}

    for _ in range(repeats):
        store = InMemoryLessonStore()
        seed_poison(store, tasks)              # rebuilt each pass: consolidate mutates it
        reports = dict(zip(ARM_KEYS, run_live(store, tasks, key="scoreboard",
                                              solver=solver, seed=seed)))
        for key, rep in reports.items():
            rates[key].append(rep.success_rate)
            for t in tasks:
                if _passed(rep, t.id):
                    passes[t.id][key] += 1

    arms = [{"key": k, "label": ARM_LABELS[k],
             "success_rate": mean(rates[k]), "min": min(rates[k]), "max": max(rates[k])}
            for k in ARM_KEYS]
    task_rows = [{"id": t.id, **{k: passes[t.id][k] for k in ARM_KEYS}} for t in tasks]
    warm_m, cold_m, naive_m = mean(rates["warm"]), mean(rates["cold"]), mean(rates["naive"])
    return {
        "model": model, "repeats": repeats, "seed": seed,
        "arms": arms, "tasks": task_rows,
        "lift": {"cold_to_warm": warm_m - cold_m, "naive_to_warm": warm_m - naive_m},
    }


def write_data_js(results: dict, out_path) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.RESULTS = " + json.dumps(results, indent=2) + ";\n",
                   encoding="utf-8")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run the Mimir scoreboard benchmark.")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    model = os.environ.get("MIMIR_CLAUDE_MODEL") or "default"
    results = build_results(TASKS, repeats=args.repeats, seed=args.seed, model=model)
    write_data_js(results, args.out)
    print(f"wrote {args.out}  (model={model}, repeats={args.repeats}, seed={args.seed})")


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && python -m pytest tests/test_scoreboard.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add bench/scoreboard.py tests/test_scoreboard.py
git commit -m "feat(bench): scoreboard orchestrator emitting data.js"
```

---

## Task 3: Self-contained scoreboard page + committed sample

**Files:**
- Create: `bench/scoreboard/index.html`
- Create: `bench/scoreboard/data.js` (committed sample; also the generated-output path)

**Interfaces:**
- Consumes: `window.RESULTS` (shape produced by Task 2's `write_data_js`).
- Produces: a static page; no Python interface.

**Important:** do NOT create `bench/scoreboard/__init__.py` (see Global Constraints — it would shadow the `bench.scoreboard` module).

- [ ] **Step 1: Create the committed sample `data.js`**

Create `bench/scoreboard/data.js` (lets the page be iterated with zero tokens; the live run overwrites it):

```javascript
window.RESULTS = {
  "model": "sonnet",
  "repeats": 3,
  "seed": 0,
  "arms": [
    { "key": "cold",  "label": "COLD (no memory)",    "success_rate": 0.0,   "min": 0.0, "max": 0.0 },
    { "key": "naive", "label": "NAIVE (ungated)",     "success_rate": 0.111, "min": 0.0, "max": 0.333 },
    { "key": "warm",  "label": "WARM (Mimir, gated)", "success_rate": 1.0,   "min": 1.0, "max": 1.0 }
  ],
  "tasks": [
    { "id": "t-flush", "cold": 0, "naive": 0, "warm": 3 },
    { "id": "t-page",  "cold": 0, "naive": 1, "warm": 3 },
    { "id": "t-unit",  "cold": 0, "naive": 0, "warm": 3 }
  ],
  "lift": { "cold_to_warm": 1.0, "naive_to_warm": 0.889 }
};
```

- [ ] **Step 2: Create `index.html`**

Create `bench/scoreboard/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Mimir — Prove It Scoreboard</title>
<style>
  :root {
    --bg: #0b0f14; --panel: #131a22; --ink: #e6edf3; --muted: #8b98a5;
    --line: #223140; --cold: #f2545b; --naive: #e0a458; --warm: #35c07f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.5 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    padding: 48px 32px;
  }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 30px; margin: 0 0 4px; letter-spacing: -0.02em; }
  .sub { color: var(--muted); margin: 0 0 28px; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 32px; }
  .chip {
    background: var(--panel); border: 1px solid var(--line); border-radius: 999px;
    padding: 4px 12px; font-size: 13px; color: var(--muted);
  }
  .chip b { color: var(--ink); font-weight: 600; }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: 24px; margin-bottom: 24px;
  }
  .panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted); margin: 0 0 20px; }
  .bar-row { display: grid; grid-template-columns: 190px 1fr 64px; align-items: center;
    gap: 16px; margin-bottom: 18px; }
  .bar-row:last-child { margin-bottom: 0; }
  .bar-label { font-size: 14px; }
  .track { position: relative; height: 26px; background: #0c1219;
    border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .fill { height: 100%; width: 0; border-radius: 7px 0 0 7px;
    transition: width 1100ms cubic-bezier(.2,.7,.2,1); }
  .band { position: absolute; top: 0; bottom: 0; background: rgba(255,255,255,.14);
    border-left: 1px solid rgba(255,255,255,.4); border-right: 1px solid rgba(255,255,255,.4); }
  .pct { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { padding: 10px 12px; text-align: center; border-bottom: 1px solid var(--line); }
  th:first-child, td:first-child { text-align: left; color: var(--muted); font-family: ui-monospace, monospace; }
  th { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
  .cell { display: inline-block; min-width: 44px; padding: 3px 8px; border-radius: 6px; font-size: 14px; }
  .pass { background: rgba(53,192,127,.16); color: var(--warm); }
  .fail { background: rgba(242,84,91,.14); color: var(--cold); }
  .mixed { background: rgba(224,164,88,.16); color: var(--naive); }
  .lift { color: var(--muted); font-size: 14px; margin-top: 8px; }
  .lift b { color: var(--warm); }
</style>
</head>
<body>
<div class="wrap">
  <h1>Mimir — does memory <em>actually</em> help?</h1>
  <p class="sub">Same agent, same tasks. The only variable is memory.</p>
  <div class="chips" id="chips"></div>

  <div class="panel">
    <h2>Task success rate</h2>
    <div id="bars"></div>
    <div class="lift" id="lift"></div>
  </div>

  <div class="panel">
    <h2>Per-task pass / fail</h2>
    <table id="grid"></table>
  </div>
</div>

<script src="data.js"></script>
<script>
  var R = window.RESULTS;
  var COLORS = { cold: getVar('--cold'), naive: getVar('--naive'), warm: getVar('--warm') };
  function getVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function pct(x) { return Math.round(x * 100) + '%'; }

  // chips
  document.getElementById('chips').innerHTML =
    chip('model', R.model === 'sonnet' ? 'Sonnet 5' : R.model) +
    chip('tasks', R.tasks.length) +
    chip('repeats', R.repeats) +
    chip('seed', R.seed);
  function chip(k, v) { return '<span class="chip">' + k + ' <b>' + v + '</b></span>'; }

  // bars
  var bars = document.getElementById('bars');
  R.arms.forEach(function (a) {
    var row = document.createElement('div');
    row.className = 'bar-row';
    row.innerHTML =
      '<div class="bar-label">' + a.label + '</div>' +
      '<div class="track"><div class="fill" style="background:' + COLORS[a.key] + '"></div>' +
      '<div class="band" style="left:' + pct(a.min) + ';width:' + pct(a.max - a.min) + '"></div></div>' +
      '<div class="pct">' + pct(a.success_rate) + '</div>';
    bars.appendChild(row);
    // animate after paint
    var fill = row.querySelector('.fill');
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { fill.style.width = pct(a.success_rate); });
    });
  });

  // lift line
  document.getElementById('lift').innerHTML =
    'Lift vs. no memory: <b>+' + pct(R.lift.cold_to_warm) + '</b> &nbsp;·&nbsp; ' +
    'vs. ungated memory: <b>+' + pct(R.lift.naive_to_warm) + '</b>';

  // grid
  var grid = document.getElementById('grid');
  var head = '<tr><th>task</th>' + R.arms.map(function (a) {
    return '<th>' + a.key.toUpperCase() + '</th>'; }).join('') + '</tr>';
  var rows = R.tasks.map(function (t) {
    return '<tr><td>' + t.id + '</td>' + R.arms.map(function (a) {
      return '<td>' + cell(t[a.key], R.repeats) + '</td>'; }).join('') + '</tr>';
  }).join('');
  grid.innerHTML = head + rows;
  function cell(k, n) {
    var cls = k === n ? 'pass' : (k === 0 ? 'fail' : 'mixed');
    var txt = n > 1 ? (k + '/' + n) : (k === 1 ? '✓' : '✗');
    return '<span class="cell ' + cls + '">' + txt + '</span>';
  }
</script>
</body>
</html>
```

- [ ] **Step 3: Verify the module still resolves (name-collision guard)**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && python -c "import bench.scoreboard as s; print(s.__file__)"`
Expected: prints a path ending in `bench/scoreboard.py` (NOT the directory). Confirms `bench/scoreboard/` does not shadow the module.

- [ ] **Step 4: Verify the sample renders (manual)**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && open bench/scoreboard/index.html`
Expected: page opens; three bars animate (COLD 0%, NAIVE ~11%, WARM 100%), a lift line, and a task grid showing `0/3`, `1/3`, `3/3` cells. Confirm no console errors (nothing is fetched over the network).

- [ ] **Step 5: Commit**

```bash
git add bench/scoreboard/index.html bench/scoreboard/data.js
git commit -m "feat(bench): self-contained scoreboard page + sample data"
```

---

## Task 4: Documentation — how to run the demo

**Files:**
- Modify: `README.md` (the benchmark section, around lines 75-89)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add the scoreboard run instructions**

In `README.md`, immediately after the existing `demo_band` code block in "The benchmark" section (after line 89), add:

````markdown
### Visual scoreboard (Sonnet 5)

Run the three arms live against Sonnet 5 and render a self-contained page:

```bash
MIMIR_CLAUDE_MODEL=sonnet python -m bench.scoreboard --repeats 3
open bench/scoreboard/index.html
```

`--repeats 3` draws 3×3 tasks per arm for a noise band (27 solver calls); use
`--repeats 1` for a quick single run (9 calls). The page reads `data.js` and
needs no server — open it directly and screen-record the bars filling in.
````

- [ ] **Step 2: Verify the commands in the doc are accurate**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && python -m bench.scoreboard --help`
Expected: usage shows `--repeats`, `--seed`, `--out` — matches the README.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document the visual scoreboard run"
```

---

## Task 5: Live smoke run (optional, spends tokens)

**Files:** none (produces `bench/scoreboard/data.js` from a real run).

**Interfaces:** none.

> Run this only when you're ready to spend Sonnet 5 tokens for the actual video numbers. Requires a working `claude` CLI logged into an account with Sonnet 5 access. This step is not part of CI and has no automated test.

- [ ] **Step 1: One-task diagnostic (1 call)**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && MIMIR_CLAUDE_MODEL=sonnet python -c "from bench.live import inspect_task; inspect_task()"`
Expected: prints prompt, raw reply, extracted code, and a score line. Confirms the CLI + model pin work end-to-end before the full run.

- [ ] **Step 2: Full scoreboard run**

Run: `cd /Users/tushar/Downloads/Projects/mirmir/mimir && MIMIR_CLAUDE_MODEL=sonnet python -m bench.scoreboard --repeats 3`
Expected: `wrote bench/scoreboard/data.js (model=sonnet, repeats=3, seed=0)`; the file now holds real numbers.

- [ ] **Step 3: Open and record**

Run: `open bench/scoreboard/index.html`
Expected: bars reflect the live run. If the WARM−COLD lift is smaller than the COLD band width, treat it as noise and re-run with more repeats (per the harness's honesty rule).

- [ ] **Step 4 (optional): Commit the real numbers**

```bash
git add bench/scoreboard/data.js
git commit -m "chore(bench): record live Sonnet 5 scoreboard numbers"
```

---

## Self-Review

**Spec coverage:**
- Model pinning (`MIMIR_CLAUDE_MODEL` → `--model`) → Task 1. ✓
- Orchestrator running 3 arms via `run_live` per repeat, aggregating per-arm bands + per-task pass counts, writing `data.js` → Task 2. ✓
- Per-task grid data from `Report.records` (not `run_live_repeated`) → Task 2 `_passed` + `build_results`. ✓
- Harness-arm → `data.js` key mapping; fractions in [0,1] → Task 2 (`ARM_KEYS`/`ARM_LABELS`), HTML formats as %. ✓
- Self-contained HTML with bars + band whisker + grid → Task 3. ✓
- Zero-token tests for both code changes → Tasks 1 & 2. ✓
- Zero-dep core preserved (stdlib + vanilla JS only) → no new deps introduced. ✓
- `bench/scoreboard/` has no `__init__.py`; module still resolves → Task 3 Step 3 guard. ✓
- Error handling inherited from harness (crash→0, `ClaudeLimitError` aborts) → unchanged; noted in spec. ✓
- Repeats knob (default 3) and model default "default" → Task 2 `main`. ✓
- Run instructions → Task 4; live run → Task 5. ✓

**Placeholder scan:** No TBD/TODO/"add error handling" placeholders; all code steps carry complete code. One correction is called out explicitly in Task 2 Step 3 (drop the `if False` guard line — replace with the plain `warm_m, cold_m, naive_m = ...` assignment).

**Type consistency:** `build_results(tasks, *, repeats, seed, model, solver) -> dict`, `write_data_js(results, out_path)`, `main(argv=None)` names match between Task 2's Interfaces, its code, and the tests. `ARM_KEYS = ("cold","naive","warm")` matches the `data.js` keys the HTML reads (`a.key`, `t[a.key]`) and the test's `arms` dict. `PASS_SCORE` imported, not hardcoded. `run_live` return order (cold, naive, warm) matches the positional `zip(ARM_KEYS, ...)`.
