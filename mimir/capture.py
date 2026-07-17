"""C1 — fast-path hook listener.

The whole point of the two-speed design: this path is O(1), local, and never
touches an LLM. Hooks fire it passively (PostToolUse / failure / SessionEnd) so
the model never has to *choose* to remember. It appends a raw EPISODE and gets
out of the way. It must never raise into the agent loop — swallow and log loudly.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from mimir.auto_consolidate import bump_failure_count
from mimir.models import Episode

log = logging.getLogger("mimir.capture")

# outcome_score from the deterministic verifier — never an LLM here.
OUTCOME_FAIL = 0.0
OUTCOME_PASS = 1.0


def from_hook(event: dict) -> Episode:
    """Map a Claude SDK hook payload to an EPISODE. A failed tool call = a MISTAKE."""
    failed = bool(event.get("is_error"))
    return Episode(
        action=event.get("tool_name", ""),
        context=json.dumps(event.get("tool_input", ""), default=str),
        consequence=json.dumps(event.get("tool_response", ""), default=str),
        outcome_score=OUTCOME_FAIL if failed else OUTCOME_PASS,
        session_id=event.get("session_id", ""),
        task_id=event.get("task_id", ""),
    )


def from_cline_hook(event: dict) -> Episode:
    """Map a Cline PostToolUse hook payload to an EPISODE. A failed tool call = a MISTAKE.

    # ponytail: Cline's docs confirm the base fields are camelCase (taskId, toolName) but
    # give the tool-call fields only in prose ("tool name, parameters, execution result"),
    # with one example snippet showing snake_case (tool_input.command). This checks both
    # conventions defensively; tighten to the real key names once verified against a live
    # Cline payload.
    """
    tool_input = event.get("tool_input", event.get("toolInput", {}))
    result = event.get("tool_response", event.get("toolResponse", event.get("result", {})))
    result_dict = result if isinstance(result, dict) else {}
    failed = bool(
        event.get("is_error")
        or result_dict.get("error")
        or result_dict.get("success") is False
    )
    task_id = event.get("taskId", event.get("task_id", ""))
    return Episode(
        action=event.get("tool_name", event.get("toolName", "")),
        context=json.dumps(tool_input, default=str),
        consequence=json.dumps(result, default=str),
        outcome_score=OUTCOME_FAIL if failed else OUTCOME_PASS,
        session_id=task_id,  # Cline has no separate session concept; a task is the closest unit
        task_id=task_id,
    )


def from_hermes_call(tool_name: str, params, result) -> Episode:
    """Map a Hermes Agent `post_tool_call(tool_name, params, result)` callback to an EPISODE.

    # ponytail: Hermes' post_tool_call isn't documented with a success/error field (a real
    # Langfuse plugin for the same hook doesn't branch on one either) — this treats an
    # exception result or a dict with a truthy "error" key as a MISTAKE, everything else
    # as a pass. Tighten once Hermes documents a real outcome signal.
    """
    failed = isinstance(result, BaseException) or (
        isinstance(result, dict) and bool(result.get("error"))
    )
    consequence = str(result) if isinstance(result, BaseException) else result
    return Episode(
        action=tool_name or "",
        context=json.dumps(params, default=str),
        consequence=json.dumps(consequence, default=str),
        outcome_score=OUTCOME_FAIL if failed else OUTCOME_PASS,
    )


def capture(episode: Episode, *, log_path: Path,
           state_path: Optional[Path] = None) -> Optional[str]:
    """Append one EPISODE to the append-only JSONL log. Returns its id, or None on failure."""
    try:
        if not episode.id:
            episode.id = uuid.uuid4().hex
        if episode.timestamp is None:
            episode.timestamp = datetime.now(timezone.utc)

        row = asdict(episode)
        row["timestamp"] = episode.timestamp.isoformat()

        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        if episode.outcome_score == OUTCOME_FAIL:
            bump_failure_count(state_path)
        return episode.id
    except Exception:  # never propagate into the agent loop; no silent failure either
        log.exception("mimir.capture failed to append EPISODE (dropped, agent unaffected)")
        return None


def run_hook(stdin_text: str, *, log_path: Path,
            mapper: Callable[[dict], Episode] = from_hook) -> int:
    """Entrypoint a hook invokes: parse one event, capture it. `mapper` adapts a runtime's
    own payload shape to an EPISODE — defaults to Claude Code's; pass `from_cline_hook` etc.
    for other runtimes.

    Returns 0 ALWAYS — the fast-path contract is that a capture failure can
    never block the agent loop. Empty stdin (e.g. SessionEnd) is a no-op.
    """
    try:
        text = stdin_text.strip()
        if text:
            capture(mapper(json.loads(text)), log_path=log_path)
    except Exception:  # swallow to honor the never-block contract; log loudly, never silent
        log.exception("mimir hook dropped a malformed event (agent unaffected)")
    return 0
