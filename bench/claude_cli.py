"""Subprocess adapter for the `claude` CLI — a real model in the loop, no API key.

Uses the Claude Code you already run: `claude -p --output-format json`, prompt on
stdin. The `_runner` seam lets tests inject a fake so the wiring is verified with
ZERO tokens; only `python -m bench.live` spends anything.

ponytail: brittle by nature (parsing CLI output). Set MIMIR_CLAUDE_BIN to override
the binary; swap this whole module for the anthropic SDK if the CLI path bites.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Optional

DEFAULT_TIMEOUT = 180  # seconds — a coding turn can be slow; fail loud, don't hang forever
RETRIES = 2            # transient CLI/rate-limit failures self-heal; a real benchmark fires many calls

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


class ClaudeLimitError(RuntimeError):
    """Subscription/session limit (HTTP 429): retrying is pointless until it resets."""


def _resolve_bin() -> str:
    return os.environ.get("MIMIR_CLAUDE_BIN") or shutil.which("claude") or "claude"


def _subprocess_runner(prompt: str, timeout: int) -> str:
    """Call the real CLI. Raises on failure with the CLI's own message (loud AND legible)."""
    args = [_resolve_bin(), "-p", "--output-format", "json"]
    model = os.environ.get("MIMIR_CLAUDE_MODEL")
    if model:                                  # pin the solver model for a benchmark (e.g. sonnet)
        args += ["--model", model]
    proc = subprocess.run(
        args,
        input=prompt, capture_output=True, text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        # the CLI reports errors (429 rate limit, session limit) inside the JSON envelope
        msg, status = proc.stderr or proc.stdout, None
        try:
            env = json.loads(proc.stdout)
            msg, status = env.get("result", msg), env.get("api_error_status")
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        cls = ClaudeLimitError if status == 429 else RuntimeError
        raise cls(f"claude exited {proc.returncode}: {str(msg)[:200]}")
    return proc.stdout


def run_claude(prompt: str, *, timeout: int = DEFAULT_TIMEOUT, retries: int = RETRIES,
               _runner: Optional[Callable[[str, int], str]] = None) -> str:
    """Send `prompt` to Claude, return the result text. `_runner` is injectable for tests.

    Retries transient failures (rate limits, flaky CLI) with exponential backoff so one
    throttled call in a long benchmark run doesn't get laundered into a 'task failure'.
    """
    runner = _runner or _subprocess_runner
    for attempt in range(retries + 1):
        try:
            raw = runner(prompt, timeout)
            break
        except ClaudeLimitError:
            raise  # session limit won't clear in seconds — fail the whole run fast
        except Exception as exc:  # noqa: BLE001 - transient CLI/network/rate-limit, retry then re-raise
            if attempt == retries:
                raise
            print(f"[claude_cli] call failed ({exc!r}); retry {attempt + 1}/{retries}",
                  file=sys.stderr)
            time.sleep(2 ** attempt)
    try:                                   # claude --output-format json -> {"result": "..."}
        return str(json.loads(raw).get("result", "")).strip()
    except (json.JSONDecodeError, AttributeError, TypeError):
        return raw.strip()                 # plain-text fallback if the envelope changes


def extract_code(text: str) -> str:
    """Pull the first fenced code block; fall back to the whole reply (best effort)."""
    m = _FENCE.search(text)
    return (m.group(1) if m else text).strip()
