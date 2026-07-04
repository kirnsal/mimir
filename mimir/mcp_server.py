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
from pathlib import Path
from typing import Callable, Optional

from mimir.capture import capture
from mimir.models import Episode, Lesson

TAU = 0.5          # FR5: confidence floor — below this a lesson is not trusted for recall
DEFAULT_K = 5      # max lessons returned
MIN_SUPPORT = 1    # a lesson backed by fewer episodes than this counts as thin support

_WORD = re.compile(r"[a-z0-9']+")


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
                outcome_score: Optional[float] = None) -> Optional[str]:
        return capture(
            Episode(action=action, context=context, consequence=consequence,
                    outcome_score=outcome_score),
            log_path=log_path,
        )
    return handler


def build_tools(store, *, tau: float = TAU,
                log_path: Optional[Path] = None) -> dict[str, Tool]:
    """The MCP tool surface (BUILD_SPEC C4). `recall` is always live; `capture`
    goes live when `log_path` is given (where to append EPISODEs). `consolidate`
    and `attribute` stay declared — their handlers need injected LLM callables
    (judge/probe/solver), bound only in a funded live run."""
    return {
        "mimir.recall": Tool(
            name="mimir.recall",
            description="Confidence-gated recall of active LESSONs relevant to a query (FR5).",
            input_schema=_schema({"query": {"type": "string"}}, ["query"]),
            handler=lambda query: recall(store, query, tau=tau),
        ),
        "mimir.attribute": Tool(
            name="mimir.attribute",
            description="Single-lesson counterfactual credit for a task (runs in the C5 harness).",
            input_schema=_schema(
                {"task": {"type": "string"}, "lesson_id": {"type": "string"}},
                ["task", "lesson_id"],
            ),
        ),
        "mimir.capture": Tool(
            name="mimir.capture",
            description="Append a raw EPISODE (fast path, C1). Hooks normally fire this.",
            input_schema=_schema(
                {"action": {"type": "string"}, "context": {"type": "string"},
                 "consequence": {"type": "string"},
                 "outcome_score": {"type": "number"}},
                ["action"],
            ),
            handler=_capture_handler(log_path) if log_path is not None else None,
        ),
        "mimir.consolidate": Tool(
            name="mimir.consolidate",
            description="Run the slow-path consolidation job over EPISODEs since a cutoff (C2).",
            input_schema=_schema({"since": {"type": "string"}}, []),
        ),
    }
