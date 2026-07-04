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
from typing import Optional

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


def capture(episode: Episode, *, log_path: Path) -> Optional[str]:
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
        return episode.id
    except Exception:  # never propagate into the agent loop; no silent failure either
        log.exception("mimir.capture failed to append EPISODE (dropped, agent unaffected)")
        return None


def run_hook(stdin_text: str, *, log_path: Path) -> int:
    """Entrypoint a Claude Code hook invokes: parse one event, capture it.

    Returns 0 ALWAYS — the fast-path contract is that a capture failure can
    never block the agent loop. Empty stdin (e.g. SessionEnd) is a no-op.
    """
    try:
        text = stdin_text.strip()
        if text:
            capture(from_hook(json.loads(text)), log_path=log_path)
    except Exception:  # swallow to honor the never-block contract; log loudly, never silent
        log.exception("mimir hook dropped a malformed event (agent unaffected)")
    return 0
