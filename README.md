# Mimir

**Agent memory that has to prove it helps.**

Most memory layers for AI agents store things and hope retrieval makes the
agent better. Mimir inverts that: every lesson it learns is evidence-gated,
signed, and benchmarked — a lesson only survives if the agent measurably
performs better *with* it than *without* it.

> Status: **v0.0.1 — in active development.** The lifecycle below works
> end-to-end; interfaces will still move.

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

## Built on Cognee

Semantic storage and retrieval run on [Cognee](https://github.com/topoteretes/cognee)'s
LanceDB vector engine (`mimir/store_cognee.py`). Lessons are embedded and
recalled through Cognee; the persisted LESSON objects remain the source of
truth and the vector index is rebuilt from them on load.

Mimir implements the full memory lifecycle, with Cognee as the semantic engine
underneath:

| Lifecycle stage | Where it lives in Mimir |
|---|---|
| **remember** | `mimir-hook` / `capture` — episodes logged from real agent sessions |
| **memify** (improve) | `mimir consolidate` — failures distilled into judged, ε-gated, HMAC-signed lessons in the Cognee-backed store |
| **recall** | `mimir-serve` — confidence-gated semantic retrieval over Cognee's LanceDB index |
| **forget** | bi-temporal lifecycle — lessons are quarantined, superseded, or retired on evidence (never hard-deleted), and excluded from recall |

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

## MCP tools

`mimir-serve` exposes the tool surface over stdio:

- `recall` — confidence-gated lesson retrieval for the current context
- `capture` — log an episode directly (when not using the hook)

`consolidate` and `attribute` run today via the CLI/bench harness and join the
MCP surface once their LLM wiring is injectable server-side.

## Development

```bash
git clone https://github.com/kirnsal/mimir && cd mimir
pip install -e '.[dev,mcp,cognee]'
pytest
```

Python ≥ 3.10. The core package is dependency-free; `mcp` and `cognee` are
optional extras imported lazily, so tests run without either installed.

## Hackathon note

Developed with Claude Code (Anthropic) as an AI coding assistant, under human
direction and review.

## License

[Apache License 2.0](LICENSE)
