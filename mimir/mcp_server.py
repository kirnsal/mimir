"""C4 — MCP retrieval server: confidence-gated recall (FR5).

Dependency-free like the rest of Mimir. The retrieval logic (`recall`) is pure
and fully tested here; the MCP tool surface is declared as schema-bearing
definitions (`build_tools`). The real transport — `mcp.FastMCP` registering
these tools, triggered from Claude Code `PreToolUse`/`SessionStart` hooks —
wraps this later, the same swap-the-adapter pattern as the Cognee-backed store.

ponytail: no `mcp` SDK dependency until live wiring needs it; the schemas here
are exactly what FastMCP consumes, so the wrapper is mechanical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from mimir.capture import capture
from mimir.models import Episode, Lesson

TAU = 0.5          # FR5: confidence floor — below this a lesson is not trusted for recall
DEFAULT_K = 5      # max lessons returned
MIN_SUPPORT = 1    # a lesson backed by fewer episodes than this counts as thin support

_WORD = re.compile(r"[a-z0-9']+")


# ponytail: bounded cache -- rule text is immutable once written (bi-temporal
# supersede writes a new Lesson, never mutates .rule in place), so caching by
# string is safe. maxsize must stay comfortably above the active-lesson count
# or LRU eviction thrashes and makes calls slower than uncached (measured);
# 20k is far past any realistic v1 store size -- raise further if that changes.
@lru_cache(maxsize=20000)
def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


@dataclass
class RecallResult:
    """What recall hands back: ranked lessons + the FR5 UNCERTAINTY flag."""

    lessons: list[Lesson] = field(default_factory=list)
    uncertain: bool = False    # True when retrieved support is thin (FR5)
    reason: str = ""


def _relevance(query_tokens: set[str], lesson: Lesson) -> int:
    return len(query_tokens & _tokens(lesson.rule))


def recall(store, query: str, *, tau: float = TAU, k: int = DEFAULT_K,
           min_support: int = MIN_SUPPORT) -> RecallResult:
    """FR5: return active, confident, on-topic, non-contradicted LESSONs for a query.

    Filters: status=active (store.active excludes superseded/quarantined/retired),
    confidence >= tau, and exclude any lesson contradicted by another active lesson.
    Raises an UNCERTAINTY flag when nothing matches or the top support is thin.
    """
    active = store.active()
    contradicted = {cid for lo in active for cid in lo.contradicts}

    # Prefer vector/semantic ranking when the store provides it (CogneeLessonStore);
    # fall back to lexical query-overlap otherwise. The FR5 gates (tau, contradiction,
    # uncertainty) apply identically to whichever ranking the store hands back.
    semantic = getattr(store, "semantic_recall", None)
    if semantic is not None:
        ranked = semantic(query, k=k * 3)  # active-only already; re-gate below
        candidates = [lo for lo in ranked
                      if lo.confidence >= tau and lo.id not in contradicted]
    else:
        qtok = _tokens(query)
        candidates = [
            lo for lo in active
            if lo.confidence >= tau
            and lo.id not in contradicted
            and _relevance(qtok, lo) > 0
        ]
        # LESSON before raw EPISODE is implicit: this store holds only LESSONs.
        candidates.sort(key=lambda lo: (_relevance(qtok, lo), lo.confidence), reverse=True)
    top = candidates[:k]

    if not top:
        return RecallResult(lessons=[], uncertain=True,
                            reason="no active lesson cleared the recall gate")
    thin = max(len(lo.supporting_episodes) for lo in top) < min_support
    return RecallResult(
        lessons=top,
        uncertain=thin,
        reason="retrieved lessons have thin supporting evidence" if thin else "",
    )


def _has_active_lessons(store) -> bool:
    """Cheap pre-check for build_tools(): is there anything for mimir.recall
    to ever return? Fails open (True) on a store error -- this check must
    never be the reason a working capability silently disappears."""
    try:
        return bool(store.active())
    except Exception:
        return True


# --- MCP tool surface --------------------------------------------------------

@dataclass
class Tool:
    """An MCP tool definition. `input_schema` is JSON Schema (MCP `inputSchema`)."""

    name: str
    description: str
    input_schema: dict
    handler: Optional[Callable] = None   # None = declared; transport binds it at integration


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _capture_handler(log_path: Path) -> Callable:
    def handler(action: str, context: str = "", consequence: str = "",
                outcome_score: Optional[float] = None,
                recalled_lesson_ids: Optional[list[str]] = None) -> Optional[str]:
        return capture(
            Episode(action=action, context=context, consequence=consequence,
                    outcome_score=outcome_score,
                    recalled_lesson_ids=recalled_lesson_ids or []),
            log_path=log_path,
        )
    return handler


def _consolidate_handler(store, log_path: Path, *,
                         judge: Optional[Callable] = None,
                         probe: Optional[Callable] = None) -> Callable:
    """`memify`: same EPISODEs-in -> judged/ε-gated/HMAC-signed-LESSONs-out pipeline
    as `mimir consolidate` (mimir/cli.py), exposed live over MCP. `judge`/`probe`
    mirror cli.py's `consolidate_main` injection points — inject fakes to test the
    wiring token-free; left None, the live Claude judge is imported lazily (needs
    the bench module / repo tree, not the packaged wheel — same graceful
    error-message fallback as the CLI if it isn't importable)."""
    def handler() -> dict:
        from mimir.cli import DEFAULT_LESSONS, _citation_key, _episodes_from_log, save_lessons
        from mimir.consolidate import consolidate as run_consolidate
        from mimir.consolidate import sweep_episodes

        episodes = _episodes_from_log(log_path)
        if not episodes:
            return {"admitted": 0, "active_lessons": len(store.active())}

        live_judge = judge
        if live_judge is None:
            try:
                from bench.claude_judge import make_live_judge  # lazy: only this tool needs it
            except ImportError as exc:
                return {"error": f"live consolidation needs the bench judge "
                                 f"(run from the repo tree): {exc}"}
            live_judge = make_live_judge()

        # ponytail: same FR3 probe placeholder as cli.py consolidate_main — a live
        # capture log has no held-out probe set; upgrade path is identical to the CLI's.
        live_probe = probe or (lambda lessons: float(len(lessons)))
        admitted = run_consolidate(episodes, store, judge=live_judge, probe=live_probe,
                                   key=_citation_key())

        # FR4: same live circuit-breaker sweep as `mimir consolidate` (cli.py) --
        # this MCP tool is the other entry point onto the same capture log.
        all_episodes = _episodes_from_log(log_path, failures_only=False)
        quarantined = sweep_episodes(store, all_episodes)

        save_lessons(store, DEFAULT_LESSONS)
        return {"admitted": len(admitted), "active_lessons": len(store.active()),
                "quarantined": len(quarantined)}
    return handler


def _forget_handler(store) -> Callable:
    def handler(lesson_id: str) -> dict:
        store.retire(lesson_id)
        return {"lesson_id": lesson_id, "status": "retired"}
    return handler


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
