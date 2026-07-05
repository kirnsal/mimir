# Mimir

<p align="center">
  <img alt="Mimir — agent memory that has to prove it helps" src="assets/mimir.png" width="640">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <a href="https://github.com/topoteretes/cognee"><img src="https://img.shields.io/badge/built%20on-Cognee-a78bfa.svg" alt="Built on Cognee"></a>
</p>

<p align="center">
  <a href="https://github.com/kirnsal/mimir/issues">Issues</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="CODE_OF_CONDUCT.md">Code of Conduct</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="LICENSE">License</a>
</p>

**Agent memory that has to prove it helps.**

Most memory layers for AI agents store things and hope retrieval makes the
agent better. Mimir inverts that: every lesson it learns is evidence-gated,
signed, and benchmarked — a lesson only survives if the agent measurably
performs better *with* it than *without* it.

> Status: **v0.0.1 — in active development.** The lifecycle below works
> end-to-end; interfaces will still move.

---

## How it works

Mimir watches an agent work, distills its failures into lessons, and serves
those lessons back — with a paper trail at every step.

```
capture (fast path)          consolidate (slow path)          recall (MCP)
─────────────────────        ─────────────────────────        ─────────────────
Claude Code hook logs   ──►  LLM judge extracts a rule   ──►  confidence-gated
EPISODEs (action /           from failure episodes;           retrieval over the
context / consequence)       contradiction check, then        Cognee/LanceDB
to append-only JSONL.        HMAC-signed LESSON written       vector store, served
Never blocks, never          to the vector store.             as MCP tools.
raises.
```

The unit of memory:

- **EPISODE** — raw experience: what the agent did, in what context, with what
  consequence, scored by a deterministic verifier.
- **LESSON** — a distilled, specific directive (never "be careful") with a
  confidence score that moves only on scored evidence, an HMAC-SHA-256
  citation back to its supporting episodes, and a bi-temporal lifecycle
  (`active → quarantined / superseded / retired` — never hard-deleted).

---

## Quickstart

```bash
pip install 'mimir[mcp,cognee]'

# 1. Capture: register the hook into Claude Code (~/.claude/settings.json)
mimir install-hook          # idempotent; --print to paste the block yourself

# 2. Work normally. Failures get logged to ~/.mimir/episodes.jsonl.

# 3. Consolidate: distill logged failures into gated, signed lessons
mimir consolidate

# 4. Serve: gated recall over MCP (stdio), backed by the same store
mimir-serve
```

What you consolidate is what gets served — both sides run on the same
Cognee/LanceDB-backed lesson store under `~/.mimir/`.

---

## Built on Cognee

Semantic storage and retrieval run on [Cognee](https://github.com/topoteretes/cognee)'s
LanceDB vector engine (`mimir/store_cognee.py`). Lessons are embedded and
recalled through Cognee; the persisted LESSON objects remain the source of
truth and the vector index is rebuilt from them on load.

Mimir implements the full memory lifecycle, with Cognee as the semantic engine
underneath:

| Lifecycle stage | Where it lives in Mimir |
|---|---|
| **remember** | `mimir install-hook` + `mimir.capture` — episodes logged from real agent sessions |
| **memify** (improve) | `mimir consolidate` / `mimir.consolidate` — failures distilled into judged, ε-gated, HMAC-signed lessons in the Cognee-backed store |
| **recall** | `mimir.recall`, served by `mimir-serve` — confidence-gated semantic retrieval over Cognee's LanceDB index |
| **forget** | `mimir.forget` — explicit, bi-temporal retirement; lessons are also auto-quarantined/superseded on contradicting evidence (never hard-deleted), and excluded from recall either way |

---

## The benchmark (why "prove" isn't a metaphor)

`bench/` contains a WARM/COLD attribution harness: the same tasks are run by
an agent with Mimir's lessons (WARM) and without (COLD), with seeded runs, a
held-out probe set, and an ε-gate — a lesson is only admitted if the measured
lift clears the noise floor. This is the core bet: memory you can't attribute
to an outcome improvement is just storage.

```bash
pytest tests/test_live.py                                    # token-free: injected fake model
python -c "from bench.live import demo_band; demo_band(3)"  # live: real Claude via your CLI, ~27 calls
```

The live run prints each arm's mean success rate with a (min, max) noise band —
a WARM−COLD lift smaller than the band is reported as noise, not a result.

### Visual scoreboard (Sonnet 5)

Run the three arms live against Sonnet 5 and render a self-contained page:

```bash
MIMIR_CLAUDE_MODEL=sonnet python -m bench.scoreboard --repeats 3
open bench/scoreboard/index.html
```

`--repeats 3` draws 3×3 tasks per arm for a noise band (27 solver calls); use
`--repeats 1` for a quick single run (9 calls). The page reads `data.js` and
needs no server — open it directly and screen-record the bars filling in.

---

## MCP tools

`mimir-serve` exposes the full lifecycle over stdio — any MCP client (Claude
Code included) can drive it directly:

- `mimir.capture` (**remember**) — log an episode directly (when not using the hook)
- `mimir.consolidate` (**memify**) — distill logged failures into judged, ε-gated,
  HMAC-signed lessons in the Cognee-backed store
- `mimir.recall` (**recall**) — confidence-gated, Cognee-ranked lesson retrieval
  for the current context
- `mimir.forget` (**forget**) — retire a lesson for good; bi-temporal, so the
  prior version stays on record for audit but is excluded from recall

`mimir.attribute` (single-lesson counterfactual credit) stays CLI/bench-only —
it needs an injected solver callable, bound only inside the C5 benchmark harness.

---

## Development

```bash
git clone https://github.com/kirnsal/mimir && cd mimir
pip install -e '.[dev,mcp,cognee]'
pytest
```

Python ≥ 3.10. The core package is dependency-free; `mcp` and `cognee` are
optional extras imported lazily, so tests run without either installed.

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). This project
follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Found a security
issue? See [SECURITY.md](SECURITY.md) rather than opening a public issue.

---

## Hackathon note

Developed with Claude Code (Anthropic) as an AI coding assistant, under human
direction and review.

## License

[Apache License 2.0](LICENSE)
