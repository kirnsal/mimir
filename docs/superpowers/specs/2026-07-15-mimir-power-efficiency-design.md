# Mimir power/efficiency increment: live ε-gate, protected lessons, digest export

Date: 2026-07-15
Status: approved, ready for implementation planning

## Context

Researching web4.ai / Conway's Automaton (github.com/Conway-Research/automaton) surfaced
a positioning point and, in the process of grounding it against Mimir's own code, a real
gap in Mimir itself: Conway's agents use raw economic survival (credit balance) as their
only fitness signal, with no attribution back to which belief or strategy caused a given
outcome. That's the "apparent vs actual success" failure mode Mimir's ε-gate exists to
close (PRD FR3). Checking whether Mimir's own live path actually closes it turned up that
it doesn't: `mimir consolidate`'s default probe in `mimir/cli.py` is
`lambda lessons: float(len(lessons))`, which increases by exactly 1 whenever a lesson is
added, so `epsilon_admit` (improvement 1.0 >= epsilon 0.05) always passes. The ε-gate is a
no-op in the live single-user CLI path today; only the benchmark (`bench/live.py`) has a
real held-out probe.

This increment fixes that, plus two smaller features suggested by contrast with Conway's
design (`SOUL.md` self-authored identity doc -> digest export; `constitution.md` immutable
core laws -> protected lessons).

## Scope

In scope:
1. Live counterfactual probe for `mimir consolidate`, replacing the len()-counting placeholder.
2. `Lesson.protected` flag exempting a lesson from auto-supersede and circuit-breaker quarantine.
3. `mimir export --digest`: markdown snapshot of active lessons to stdout.
4. PRD.md positioning paragraph (web4.ai/Conway as a second concrete example of the
   attribution gap Mimir closes — private doc, not the public README, per the
   established repo-hygiene rule).

Out of scope (considered, rejected):
- Full solver-replay probe (re-running the failed task live) — no "replay this episode as
  a task" concept exists for arbitrary user episodes; 2x the live-call cost for marginal
  rigor gain over the judge-based counterfactual check. Revisit if the judge-based probe's
  false-admit rate turns out to matter in practice.
- CLI/MCP surface for protect/unprotect — no pinning workflow exists yet to justify one;
  store-level API only (YAGNI).
- Wallets, payments, self-replication, identity provisioning (Conway-specific) — orthogonal
  to a memory/attribution layer.

## Design

### 1. Live ε-gate probe

`bench/claude_judge.py` gets a new function alongside `make_live_judge`:

```python
def make_live_counterfactual_probe(held_out: list[Episode], *, runner=None):
    """FR3 live probe: for each held-out episode, ask the judge whether the given
    lesson set would likely have prevented its failure. Score = fraction 'yes'.
    Same _runner injection seam as make_live_judge — zero tokens in tests."""
```

One structured judge call per held-out episode (not per lesson-set-size), reusing the
existing `run_claude` / `_parse_verdict`-style tolerant JSON parsing, fail-closed on
unparseable output (counts as "no").

`consolidate_main` in `mimir/cli.py` splits the log's failure episodes: last third
(minimum 1, only if total >= 2) becomes `held_out`; the rest go to `extract()`. With
fewer than 2 episodes, `held_out = []`, the probe returns 0.0 for both baseline and
improved, and nothing admits — fail-closed, matching `make_solver_probe`'s existing
`if not held_out: return 0.0` convention. This is correct behavior, not a bug: no
held-out evidence means no counterfactual proof means no admission.

### 2. Protected lessons

- `mimir/models.py`: `Lesson.protected: bool = False`.
- `mimir/store.py`: `InMemoryLessonStore.protect(lesson_id)` / `.unprotect(lesson_id)`,
  mirroring the existing `retire`/`rollback` pair.
- `mimir/consolidate.py`: the supersede-target search in `consolidate()`
  (`next((a for a in active if detect_contradiction(a, lesson)), None)`) excludes
  lessons where `protected=True`. `circuit_breaker_sweep()` skips protected lessons
  even when their adoption stats would otherwise quarantine them.

No CLI or MCP surface this round — set via the store API directly (or a short script),
matching the YAGNI call: there's no pinning workflow yet to build a command for.

### 3. Digest export

New `export_main(argv)` in `mimir/cli.py`, dispatched from `main()` alongside
`consolidate`/`serve`/`hook`. `mimir export --digest`:
- `build_store()` (same Cognee/LanceDB-backed store as `serve`/`consolidate`)
- renders `store.active()` sorted by `confidence` descending as markdown:
  `- **{rule}** (confidence: {confidence:.2f}, id: {id})`
- prints to stdout (no new file-writing path; redirect with `>` for a file, consistent
  with the existing `install-hook --print` convention in this CLI)

### 4. PRD note

One paragraph in `PRD.md` §2 (same section as the existing LeCun/AI-2040 note): Conway's
Automaton as a second concrete, funded example of outcome-without-attribution, plus a
one-line self-critical note that Mimir's own CLI had the identical gap until this fix.
Private doc only — this framing does not go in the public `mimir/README.md`.

## Testing

One test per feature, following the repo's existing style (plain `pytest`, injected
fakes, no new frameworks/fixtures):
- `test_claude_judge.py` (or extend existing bench tests): live probe scores held-out
  episodes via injected `_runner`, fail-closed on unparseable judge output, returns 0.0
  for empty `held_out`.
- `test_cli.py`: `consolidate_main` with < 2 logged episodes admits nothing even when
  the injected judge passes everything; with >= 2, held-out split happens and a
  helpful-lesson-only probe admits while a useless one doesn't (reuse the
  `test_epsilon_gate_rejects_lesson_that_does_not_help_probe` pattern from
  `test_consolidate.py`, wired through the CLI's split logic).
- `test_store.py`: `protect`/`unprotect` round-trip; a protected lesson is excluded as
  a supersede target and from `circuit_breaker_sweep`'s quarantine list.
- `test_cli.py`: `export_main` output contains expected markdown for a store with two
  active lessons of differing confidence, in descending order.

## Open questions

None — all decisions confirmed during brainstorming (see conversation log 2026-07-15).
