# Recall Retrieval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Skip the `mimir.recall` MCP tool-call round-trip entirely when the
lesson store has zero active lessons, instead of exposing a tool that's
guaranteed to come back empty.

**Architecture:** A pure helper `_has_active_lessons(store) -> bool`
(fail-open on any store error) gates whether `build_tools()` includes the
`"mimir.recall"` key in its returned dict. Same shape as the existing
`handler=None` pattern already used for `mimir.capture`/`mimir.consolidate`
when `log_path` is missing, just applied to the whole tool entry instead of
just the handler.

**Tech Stack:** Python, stdlib only (no new dependencies) — matches the
rest of `mimir/mcp_server.py`.

## Global Constraints

- Dependency-free: no new imports beyond stdlib (matches existing module
  docstring: "Dependency-free like the rest of Mimir").
- Fail open on error: `_has_active_lessons` must never turn a store error
  into a silently missing tool (spec: "Error handling").
- No change to `recall()`'s own gating logic (tau, contradiction exclusion,
  `uncertain` flag) — out of scope per spec's "Explicit non-goals".
- No change to `mimir/hermes_memory.py` or `mimir/cli.py` — scoped to
  `mimir/mcp_server.py` only, per spec's "Explicit non-goals".

---

## Task 1: Add the retrieval gate to `build_tools()`

**Files:**
- Modify: `mimir/mcp_server.py:53-233` (add `_has_active_lessons` after
  `recall()`, restructure `build_tools()`'s return)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `_has_active_lessons(store) -> bool` — a private module-level
  function in `mimir/mcp_server.py`. Not consumed by any other module; used
  only inside `build_tools()`.

- [ ] **Step 1: Write the failing tests**

Add these three tests to `tests/test_mcp_server.py`, in the "MCP tool
surface" section (after `test_tool_surface_validates_and_recall_is_wired`,
around line 89, before `test_capture_tool_is_live_when_log_path_given`):

```python
def test_recall_tool_omitted_when_store_has_no_active_lessons():
    tools = M.build_tools(_store())  # empty store, no active lessons
    assert "mimir.recall" not in tools


def test_recall_tool_present_when_store_has_active_lessons():
    store = _store(_lesson("use backoff on 429"))
    tools = M.build_tools(store)
    assert "mimir.recall" in tools


def test_recall_tool_present_when_active_lessons_check_raises():
    class _BoomStore:
        def active(self):
            raise RuntimeError("store unavailable")

    tools = M.build_tools(_BoomStore())
    assert "mimir.recall" in tools
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::test_recall_tool_omitted_when_store_has_no_active_lessons tests/test_mcp_server.py::test_recall_tool_present_when_store_has_active_lessons tests/test_mcp_server.py::test_recall_tool_present_when_active_lessons_check_raises -v`

Expected: `test_recall_tool_omitted_when_store_has_no_active_lessons` FAILS
(currently `"mimir.recall"` is always present, even for an empty store —
that's exactly the gap this task closes). The other two currently PASS
already (today's `build_tools()` always includes `mimir.recall`
regardless of store contents) — that's expected and fine; they exist as
regression guards for after Step 3's change, not as new-behavior proofs.

- [ ] **Step 3: Implement `_has_active_lessons` and wire it into `build_tools()`**

In `mimir/mcp_server.py`, insert this function immediately after the
`recall()` function (after its closing line, before the
`# --- MCP tool surface ---` comment currently at line 95):

```python
def _has_active_lessons(store) -> bool:
    """Cheap pre-check for build_tools(): is there anything for mimir.recall
    to ever return? Fails open (True) on a store error -- this check must
    never be the reason a working capability silently disappears."""
    try:
        return bool(store.active())
    except Exception:
        return True
```

Then replace the entire `build_tools()` function (currently lines
180-233) with:

```python
def build_tools(store, *, tau: float = TAU,
                log_path: Optional[Path] = None,
                consolidate_judge: Optional[Callable] = None,
                consolidate_probe: Optional[Callable] = None) -> dict[str, Tool]:
    """The MCP tool surface (BUILD_SPEC C4): remember (capture) / memify (consolidate)
    / recall / forget, all live. `recall` is live but only exposed when the store has
    active lessons to return (retrieval gate -- skips a guaranteed-empty round-trip,
    e.g. right after install before consolidate has ever run); `forget` is always
    live. `capture` and `consolidate` go live when `log_path` is given (where
    EPISODEs are appended/read). `attribute` stays declared-only — it needs an
    injected solver callable, bound only inside the C5 benchmark harness."""
    tools: dict[str, Tool] = {}
    if _has_active_lessons(store):
        tools["mimir.recall"] = Tool(
            name="mimir.recall",
            description="Confidence-gated recall of active LESSONs relevant to a query (FR5).",
            input_schema=_schema({"query": {"type": "string"}}, ["query"]),
            handler=lambda query: recall(store, query, tau=tau),
        )
    tools["mimir.attribute"] = Tool(
        name="mimir.attribute",
        description="Single-lesson counterfactual credit for a task (runs in the C5 harness).",
        input_schema=_schema(
            {"task": {"type": "string"}, "lesson_id": {"type": "string"}},
            ["task", "lesson_id"],
        ),
    )
    tools["mimir.capture"] = Tool(
        name="mimir.capture",
        description="Append a raw EPISODE (fast path, C1). Hooks normally fire this.",
        input_schema=_schema(
            {"action": {"type": "string"}, "context": {"type": "string"},
             "consequence": {"type": "string"},
             "outcome_score": {"type": "number"},
             "recalled_lesson_ids": {"type": "array", "items": {"type": "string"}}},
            ["action"],
        ),
        handler=_capture_handler(log_path) if log_path is not None else None,
    )
    tools["mimir.consolidate"] = Tool(
        name="mimir.consolidate",
        description="memify: distill logged failure EPISODEs into judged, ε-gated, "
                    "HMAC-signed LESSONs and persist them (C2).",
        input_schema=_schema({}, []),
        handler=_consolidate_handler(store, log_path,
                                    judge=consolidate_judge,
                                    probe=consolidate_probe)
                if log_path is not None else None,
    )
    tools["mimir.forget"] = Tool(
        name="mimir.forget",
        description="forget: retire a LESSON for good. Bi-temporal — the prior version "
                    "stays on record (FR7 audit trail), but it's excluded from recall.",
        input_schema=_schema({"lesson_id": {"type": "string"}}, ["lesson_id"]),
        handler=_forget_handler(store),
    )
    return tools
```

This is a mechanical restructure of the existing dict-literal `return` into
incremental `tools[...] = Tool(...)` assignments — every existing tool
entry (`mimir.attribute`, `mimir.capture`, `mimir.consolidate`,
`mimir.forget`) keeps its exact existing body, just reformatted as an
assignment. Only `mimir.recall`'s entry becomes conditional.

- [ ] **Step 4: Run the full test file to verify everything passes**

Run: `pytest tests/test_mcp_server.py -v`

Expected: all tests PASS, including the three new ones and all
pre-existing tests (`test_tool_surface_validates_and_recall_is_wired`
seeds a lesson via `_store(_lesson(...))` so `mimir.recall` stays present
for that test; `test_forget_tool_retires_a_lesson` also seeds a lesson
before calling `build_tools()`, so it's unaffected too — no existing test
in this file calls `build_tools()` on an empty store while expecting
`mimir.recall` to be present).

- [ ] **Step 5: Run the full project test suite as a regression check**

Run: `pytest -q`

Expected: all tests PASS (no other module touches `build_tools()`'s return
shape in a way that assumes `mimir.recall` is unconditionally present).

- [ ] **Step 6: Commit**

```bash
git add mimir/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: gate mimir.recall tool exposure on store having active lessons"
```
