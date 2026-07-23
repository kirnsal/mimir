# Mimir

<p align="center">
  <img alt="Mimir — the agent memory that has to earn its keep" src="assets/mimir.png" width="640">
</p>

<p align="center">
  <a href="https://github.com/kirrns/mimir/actions/workflows/tests.yml"><img src="https://github.com/kirrns/mimir/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
</p>

<p align="center">
  <a href="https://github.com/kirrns/mimir/issues">Issues</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="CODE_OF_CONDUCT.md">Code of Conduct</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="LICENSE">License</a>
</p>

**The agent memory that has to earn its keep.**

You've felt this: your coding agent nails a fix, you move on, and three
sessions later it makes the *exact same mistake* again. Every "memory" tool
promises to fix this by hoarding everything it sees — every file, every
message, every guess — and hoping that helps next time. Most of the time it
doesn't. It just makes retrieval slower.

Mimir does the opposite. It watches your agent fail, turns that failure into
one specific, testable lesson — a skill your agent actually *earns*, not a
memory dump — and keeps it only if a real before/after benchmark *proves*
the agent performs measurably better with it. No proof, no lesson. Ever.
Every lesson that does survive is HMAC-signed and traceable back to the
failure that created it, so nothing enters your agent's context without a
paper trail.

Install it once. Capture runs quietly in the background of every Claude Code
session — it never blocks, never raises. From then on your agent gets
sharper the more you use it, and you can always see exactly which lesson
fixed which mistake.

> **v0.1.0.** The full lifecycle — capture, consolidate, recall, forget —
> works end-to-end and is covered by CI. Pre-1.0: interfaces can still move.

---

## How it works

Mimir watches an agent work, distills its failures into lessons, and serves
those lessons back — with a paper trail at every step.

```
capture (fast path)          consolidate (slow path)          recall (MCP)
─────────────────────        ─────────────────────────        ─────────────────
Claude Code hook logs   ──►  LLM judge extracts a rule   ──►  confidence-gated
EPISODEs (action /           from failure episodes;           retrieval over the
context / consequence)       contradiction check, then        LanceDB
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

## What this looks like day to day

- **You install it once.** The hook logs failures in the background; you
  never think about it again during a normal session.
- **Consolidation happens on its own.** Once enough new failures pile up
  (5 by default) and enough time has passed since the last run (4 hours
  by default), the next hook call quietly spawns a background
  `mimir consolidate` for you — no command to remember. Run
  `mimir consolidate` yourself any time for an on-demand pass, or set
  `MIMIR_AUTO_CONSOLIDATE=0` to go back to fully manual.
- **From then on, recall is automatic.** Every session after that, the
  agent pulls in whatever lessons actually clear the bar for the context
  it's in — you don't ask for it, you just notice fewer repeat mistakes.
- **You can always audit why.** Every lesson traces back to the specific
  failure and the benchmark that proved it helped, and every
  auto-consolidate run is logged to `~/.mimir/auto_consolidate.log` —
  `mimir.forget` retires a lesson instantly if it ever stops earning its
  keep.

---

## The benchmark (why "prove" isn't a metaphor)

`bench/` contains a WARM/COLD attribution harness: the same tasks are run by
an agent with Mimir's lessons (WARM) and without (COLD), with seeded runs, a
held-out probe set, and an ε-gate — a lesson is only admitted if the measured
lift clears the noise floor. This is the core bet: memory you can't attribute
to an outcome improvement is just storage.

|  | Typical memory layer | Mimir |
|---|---|---|
| What gets kept | Everything it sees | Only what clears the ε-gate |
| Evidence | "Should help" | WARM vs. COLD, seeded, noise-banded |
| Can you reproduce the claim? | Usually not — take the vendor's word | Yes — `demo_band()` is one command, on your machine |
| Traceability | Opaque blob | HMAC-signed, cited back to the failing episode |

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

## Quickstart

```bash
pip install 'mimir-agent-memory[mcp]' && mimir setup
```

One command: it registers the capture hook into Claude Code
(`~/.claude/settings.json`) and registers `mimir-serve` as an MCP server
(via `claude mcp add`, if the `claude` CLI is on `PATH` — otherwise it says
so and capture still works on its own). From here:

```bash
# Work normally. Failures get logged to ~/.mimir/episodes.jsonl.

# Consolidate: happens automatically in the background once enough
# failures pile up (see "day to day" above) -- or run it yourself:
mimir consolidate
```

Lessons then come back automatically through `mimir.recall` over MCP — what
you consolidate is what gets served, both sides run on the same
LanceDB-backed lesson store under `~/.mimir/`.

Prefer to do it by hand, or your client isn't the `claude` CLI? `mimir
install-hook` registers just the capture hook, and `mimir-serve` is a
stdio MCP server you can point any MCP client at directly.

**Windows:** pip installs the `mimir`/`mimir-hook`/`mimir-serve` commands into
`%APPDATA%\Python\Python3XX\Scripts`, which often isn't on `PATH` by default —
pip will warn about this at install time. If `mimir` isn't found afterward,
add that folder to `PATH` (or run everything as `python -m mimir.cli ...`).

**Also on Cline and Hermes:** `mimir install-hook --cline` writes the
`PostToolUse` hook script Cline picks up automatically (capture only).
Hermes gets two drop-in plugins: `hermes_plugin/` captures tool-call
failures the same way, and `hermes_memory_plugin/` goes further — it
registers Mimir as an actual Hermes `MemoryProvider`, so `mimir.recall`
serves lessons straight into Hermes' prompt (`memory.provider: mimir` in
Hermes' `config.yaml`). New integrations, schemas not yet verified against a
live session; [open an issue](https://github.com/kirrns/mimir/issues) if
something doesn't map correctly.

**Anything else:** if your tool can run a shell command with JSON on
stdin, `mimir hook --config <path>` plugs it in without touching Mimir's
code — see [docs/integrations/generic.md](docs/integrations/generic.md).

---

## Semantic storage

Semantic storage and retrieval run directly on [LanceDB](https://github.com/lancedb/lancedb)
(`mimir/store_semantic.py`) — no framework in between, just a thin, swappable
`VectorIndex` seam (LanceDB for a real on-disk store, or a zero-dependency
in-process cosine index for tests and low-footprint runs). The persisted
LESSON objects remain the source of truth; the vector index is a derived
cache, rebuilt from them on load.

The default embedder is a dependency-free token-hashing bag-of-words (good
enough for shared-vocabulary matches, zero install cost). For real semantic
recall, opt into a local [fastembed](https://github.com/qdrant/fastembed)
model (ONNX, no torch, no network after the first download):

```bash
pip install 'mimir-agent-memory[embed]'
export MIMIR_EMBED_MODEL=BAAI/bge-small-en-v1.5   # any fastembed model name
```

Mimir implements the full memory lifecycle:

| Lifecycle stage | Where it lives in Mimir |
|---|---|
| **remember** | `mimir install-hook` + `mimir.capture` — episodes logged from real agent sessions |
| **memify** (improve) | `mimir consolidate` / `mimir.consolidate` — failures distilled into judged, ε-gated, HMAC-signed lessons in the LanceDB-backed store |
| **recall** | `mimir.recall`, served by `mimir-serve` — confidence-gated semantic retrieval over the LanceDB index |
| **forget** | `mimir.forget` — explicit, bi-temporal retirement; lessons are also auto-quarantined/superseded on contradicting evidence (never hard-deleted), and excluded from recall either way |

---

## MCP tools

`mimir-serve` exposes the full lifecycle over stdio — any MCP client (Claude
Code included) can drive it directly:

- `mimir.capture` (**remember**) — log an episode directly (when not using the hook)
- `mimir.consolidate` (**memify**) — distill logged failures into judged, ε-gated,
  HMAC-signed lessons in the LanceDB-backed store
- `mimir.recall` (**recall**) — confidence-gated, semantically-ranked lesson
  retrieval for the current context. Not exposed at all when there's nothing
  to recall yet (e.g. right after install, before the first consolidate) —
  no wasted round-trip on a call that could only ever come back empty.
- `mimir.forget` (**forget**) — retire a lesson for good; bi-temporal, so the
  prior version stays on record for audit but is excluded from recall

`mimir.attribute` (single-lesson counterfactual credit) stays CLI/bench-only —
it needs an injected solver callable, bound only inside the C5 benchmark harness.

---

## Development

```bash
git clone https://github.com/kirrns/mimir && cd mimir
pip install -e '.[dev,mcp]'
pytest
```

Python ≥ 3.10. The core package is dependency-free; `mcp` (protocol) and
`lancedb` (vector store) are the `mcp` extra, imported lazily so tests run
without either installed.

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). This project
follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Found a security
issue? See [SECURITY.md](SECURITY.md) rather than opening a public issue.

---

## License

[Apache License 2.0](LICENSE)
