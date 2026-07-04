#!/usr/bin/env python3
"""C1 hooks shim — the script a Claude Code hook actually invokes.

Wire it into ~/.claude/settings.json (PostToolUse / SessionEnd):

    {"hooks": {"PostToolUse": [{"hooks": [{"type": "command",
       "command": "python C:/Users/HP/Downloads/0xkirxn-project-main/mimir/hooks/capture_hook.py"}]}]}}

It reads the hook event JSON on stdin, appends one EPISODE, and exits 0 no
matter what — a capture failure must never block the agent loop. Override the
log location with the MIMIR_EPISODE_LOG env var.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# allow running as a bare script without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir.capture import run_hook  # noqa: E402

DEFAULT_LOG = Path.home() / ".mimir" / "episodes.jsonl"


def _log_path() -> Path:
    return Path(os.environ.get("MIMIR_EPISODE_LOG", str(DEFAULT_LOG)))


if __name__ == "__main__":
    sys.exit(run_hook(sys.stdin.read(), log_path=_log_path()))
