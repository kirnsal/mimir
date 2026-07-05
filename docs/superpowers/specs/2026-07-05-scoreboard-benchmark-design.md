# Mimir "Prove It" Scoreboard — Design Spec

**Date:** 2026-07-05
**Status:** Approved (brainstorming), pending spec review
**Author:** Tushar Karkera (with Claude Code)

## Purpose

Produce a video-ready visual benchmark for a hackathon demo that proves Mimir's
agent memory layer measurably helps a real model. It runs the existing 3-arm
WARM/COLD attribution harness live against **Sonnet 5**, captures the results to
a data file, and renders a single self-contained HTML "scoreboard" page — three
animated bars plus a per-task pass/fail grid — that can be opened in a browser
and screen-recorded.

This is a presentation/wiring layer on top of the existing benchmark. It does
**not** re-implement the benchmark; the harness, recall gate, verifier, and
solver already exist in `bench/`.

## Goals

- Run the benchmark live against Sonnet 5 specifically (currently the CLI adapter
  uses whatever the Claude Code default model is).
- Show three arms — COLD, NAIVE, WARM — with an honest noise band.
- Render one clean, self-contained HTML page (no external libraries, no server)
  suitable for screen recording.
- Keep the existing, tested harness code unchanged except for one small,
  backwards-compatible edit to pin the model.
- Preserve the repo's zero-dependency core (no new runtime deps).

## Non-Goals

- No changes to the memory lifecycle, recall gate, consolidation, or scoring logic.
- No web server, build step, or JS framework.
- No new benchmark tasks — reuse the existing `bench/live.py` task set.
- No multi-model comparison (Opus vs Sonnet). This demo is Sonnet 5 only.

## Background: what already exists

- `bench/harness.py` — `run(tasks, solver, recall=..., arm=..., seed=...)` runs a
  task set through a solver and scores each with a deterministic verifier.
  `Report.records` already stores per-task `{task_id, score, n_lessons, error}`,
  so per-task pass/fail data is available with no harness change.
- `bench/live.py` — the live Sonnet solver (`cli_solver`), the fictional-API
  `TASKS` (3 tasks), `seed_poison`, `run_live` (one round, returns per-arm
  `Report`s), and `run_live_repeated`/`demo_band` (repeated noise band).
  **Note:** `run_live_repeated` returns only per-arm success-rate bands
  (`{cold, naive, warm, lift_mean}`) and discards each `Report.records`, so it
  cannot supply the per-task grid — the orchestrator iterates `run_live` per
  repeat and aggregates `Report.records` itself.
- `bench/claude_cli.py` — subprocess adapter calling `claude -p --output-format
  json`. It does **not** currently pass `--model`.
- `mimir/mcp_server.py` — `recall(store, query, ...)`, the confidence-gated
  retrieval used for the WARM arm.

### The three arms (the demo narrative)

| Arm | Meaning | Expected outcome |
|---|---|---|
| **COLD** | Sonnet 5, no memory | Fails the fictional-API tasks — the contracts don't exist in training data |
| **NAIVE** | memory on, *ungated* (seeded with a poisoned lesson) | Misled by the bad lesson → also fails |
| **WARM** | Mimir's *gated* memory | Gate rejects the poison, recalls the correct lesson → passes |

- COLD → WARM lift = "does memory help."
- NAIVE → WARM lift = "why the confidence gate matters" — the differentiator vs
  naive context-stuffing.

## Design

### Component 1 — Pin the model (`bench/claude_cli.py`)

A small, backwards-compatible edit to `_subprocess_runner`:

- Read `MIMIR_CLAUDE_MODEL` from the environment.
- If set and non-empty, append `--model <value>` to the `claude -p
  --output-format json` argument list.
- If unset, behavior is identical to today (uses the Claude Code default).

For the demo the run is invoked with `MIMIR_CLAUDE_MODEL=sonnet` so every solver
call goes to Sonnet 5. This is the only change to existing code.

### Component 2 — Orchestrator (`bench/scoreboard.py`, new)

Pure orchestration over the existing harness. Responsibilities:

- Run COLD, NAIVE, and WARM arms for `repeats` rounds (default 3) by calling
  `run_live` per repeat (using the live Sonnet solver from `bench/live.py`,
  `seed_poison` for the NAIVE arm's poisoned store, and `recall()` for the WARM
  arm), and keep each round's per-arm `Report`s.
- Aggregate, from the retained `Report`s, per arm:
  - mean success rate across repeats,
  - min and max success rate across repeats (the noise band),
  - per-task pass count across repeats (`k` passes out of `n=repeats`), computed
    from each `Report.records` — `run_live_repeated` cannot be reused here because
    it discards `records`.
- Map the harness arm labels (`COLD="cold"`, `COLD_NAIVE="cold+naive"`,
  `WARM="warm"`) to the `data.js` keys (`cold` / `naive` / `warm`).
- Emit success-rate / band values as fractions in `[0.0, 1.0]`; the HTML formats
  them as percentages.
- Write results to `bench/scoreboard/data.js` as
  `window.RESULTS = { ...json... };`.
  - Writing a `.js` global (not `.json`) lets `index.html` open by double-click
    with a `<script src="data.js">` include — no local server, no `file://`
    `fetch()`/CORS issue.
- Expose an injectable-solver seam (mirroring `bench/live.py`) so the whole
  orchestrator can run with a scripted fake solver at **zero token cost** for
  tests and for iterating on the HTML.

CLI: `python -m bench.scoreboard [--repeats N] [--seed S] [--out PATH]`.

#### `data.js` shape (contract between Components 2 and 3)

```js
window.RESULTS = {
  "model": "sonnet",            // echoed from MIMIR_CLAUDE_MODEL (or "default")
  "repeats": 3,
  "seed": 0,
  "arms": [
    { "key": "cold",  "label": "COLD (no memory)",     "success_rate": 0.0, "min": 0.0, "max": 0.2 },
    { "key": "naive", "label": "NAIVE (ungated)",      "success_rate": 0.2, "min": 0.0, "max": 0.4 },
    { "key": "warm",  "label": "WARM (Mimir, gated)",  "success_rate": 1.0, "min": 0.8, "max": 1.0 }
  ],
  "tasks": [
    { "id": "t-flush", "cold": 0, "naive": 0, "warm": 3 },   // values are pass counts out of `repeats`
    { "id": "t-page",  "cold": 0, "naive": 1, "warm": 3 }
  ],
  "lift": { "cold_to_warm": 1.0, "naive_to_warm": 0.8 }
}
```

### Component 3 — Scoreboard page (`bench/scoreboard/index.html`, new)

Self-contained HTML/CSS/JS, no external libraries or fonts fetched over the
network. Loads `data.js` via `<script>` and renders:

- **Header:** title, `model: Sonnet 5`, task count, seed, repeats.
- **Bars:** three horizontal bars (COLD / NAIVE / WARM) that animate up to their
  `success_rate`, each labeled with the percentage and a whisker drawn from `min`
  to `max` (the noise band).
- **Grid:** rows = tasks, columns = arms, each cell shows ✓/✗; when `repeats > 1`
  the cell shows `k/n` (e.g. `3/3`, `0/3`) and is colored by pass fraction.
- Dark, high-contrast theme sized for legible screen recording.

The page is static; a committed sample `data.js` lets the visuals be iterated
without spending tokens.

## Data flow

```
MIMIR_CLAUDE_MODEL=sonnet python -m bench.scoreboard --repeats 3
    │  (spends Sonnet 5 tokens once; 3 tasks × 3 arms × 3 repeats = 27 solver calls)
    ▼
bench/scoreboard/data.js   (window.RESULTS = {...})
    │
    ▼
open bench/scoreboard/index.html  →  screen-record the bars filling in
```

## Testing

- `tests/test_scoreboard.py` (zero tokens): inject a scripted solver into the
  orchestrator, run it, and assert:
  - `data.js` is written and parses to the documented shape,
  - all three arms are present with `min <= success_rate <= max`,
  - `warm.success_rate >= cold.success_rate`,
  - per-task pass counts are within `[0, repeats]`.
- HTML: eyeballed against a committed sample `data.js`; not unit-tested.

## Error handling

Inherited from the existing harness:

- A solver crash scores the task 0.0 and logs to stderr (does not crash the run).
- A `ClaudeLimitError` (HTTP 429 / session limit) aborts the run loudly rather
  than laundering rate limits into fake task failures.
- The noise band (min/max across repeats) exposes run-to-run variance honestly; a
  WARM−COLD lift smaller than the band should be read as noise.

## Configuration knobs

- `--repeats` (default **3**): with 3 tasks × 3 arms, 3 repeats = 27 Sonnet
  calls (a credible noise band); 1 repeat = 9 calls (faster/cheaper single run).
- Arms: all **3** are kept (NAIVE is the differentiator). Dropping to plain
  COLD/WARM is possible but not the default.
- `MIMIR_CLAUDE_MODEL` (default unset → Claude Code default): set to `sonnet` for
  the demo.

## Files touched

- `bench/claude_cli.py` — modified (model pinning, ~2 lines).
- `bench/scoreboard.py` — new (orchestrator).
- `bench/scoreboard/index.html` — new (self-contained page).
- `bench/scoreboard/data.js` — generated output; a committed sample for iteration.
- `tests/test_scoreboard.py` — new (zero-token test).
