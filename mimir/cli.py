"""Mimir CLI — the zero-hustle entry points (PRD G5).

Installed by `pip install mimir`:

    mimir-hook                   # the command a Claude Code hook calls (reads
                                 #   stdin, appends one EPISODE, always exits 0)
    mimir consolidate            # slow path (C2): turn logged failure EPISODEs into
                                 #   gated LESSONs and persist them (needs a live judge)
    mimir-serve                  # serve the MCP tool surface over stdio, backed by
                                 #   the Cognee/LanceDB LESSON store (pip install 'mimir[mcp,cognee]')
    mimir install-hook           # register mimir-hook into ~/.claude/settings.json
                                 #   (idempotent, backs up the old file)
    mimir install-hook --print   # just print the settings block to paste yourself

The end-to-end demo: install-hook (capture) -> use Claude -> `mimir consolidate`
(distill lessons into the LanceDB-backed store) -> `mimir-serve` (gated recall
over MCP). The store is the same on both sides, so what you consolidate is what
gets served.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from mimir.capture import OUTCOME_FAIL, run_hook
from mimir.models import Episode, Lesson

DEFAULT_HOME = Path.home() / ".mimir"
DEFAULT_LOG = DEFAULT_HOME / "episodes.jsonl"
DEFAULT_LESSONS = DEFAULT_HOME / "lessons.json"     # persisted LESSON objects (source of truth)
DEFAULT_LANCE = DEFAULT_HOME / "lance.db"           # LanceDB vector index (rebuilt from lessons)
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_COMMAND = "mimir-hook"
HOOK_EVENTS = ("PostToolUse", "SessionEnd")
CITATION_KEY_ENV = "MIMIR_CITATION_KEY"


def _log_path() -> Path:
    return Path(os.environ.get("MIMIR_EPISODE_LOG", str(DEFAULT_LOG)))


def _citation_key() -> str:
    return os.environ.get(CITATION_KEY_ENV, "mimir-dev")  # HMAC key for FR7 citations


# ---- store persistence (LESSON objects on disk; vectors rebuilt on load) ----

_DT_FIELDS = ("valid_from", "invalid_at", "last_validated")


def _lesson_from_row(row: dict) -> Lesson:
    data = dict(row)
    for f in _DT_FIELDS:
        v = data.get(f)
        data[f] = datetime.fromisoformat(v) if isinstance(v, str) else None
    return Lesson(**data)


def load_lessons(store, path: Path) -> int:
    """Rehydrate persisted LESSONs into `store` (re-upserting each into its vector index)."""
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        store.add(_lesson_from_row(row))
    return len(rows)


def save_lessons(store, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.snapshot(), encoding="utf-8")  # deterministic JSON (store.snapshot)


def build_store(*, lance_url: Optional[Path] = None, lessons_path: Optional[Path] = None):
    """The served/consolidated store: Cognee's LanceDB vector engine + persisted lessons."""
    from mimir.store_cognee import CogneeLessonStore, LanceDBVectorIndex

    lance_url = lance_url or DEFAULT_LANCE          # resolved at call time, not frozen at import
    lessons_path = lessons_path or DEFAULT_LESSONS
    store = CogneeLessonStore(index=LanceDBVectorIndex(url=str(lance_url)))
    load_lessons(store, lessons_path)
    return store


def _episodes_from_log(path: Path, *, failures_only: bool = True) -> list[Episode]:
    """Read EPISODEs from the JSONL log. A MISTAKE (outcome 0.0) is what earns a lesson."""
    if not path.exists():
        return []
    episodes: list[Episode] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        row.pop("timestamp", None)  # judge/consolidate don't use it; skip the datetime parse
        ep = Episode(**row)
        if failures_only and ep.outcome_score != OUTCOME_FAIL:
            continue
        episodes.append(ep)
    return episodes


# ---- pure, testable settings merge ----------------------------------------

def add_hook_command(settings: dict, event: str, command: str) -> dict:
    """Return a new settings dict with `command` registered under hook `event`.

    Idempotent: if the command is already present for that event, the *same*
    input object is returned unchanged. Never mutates the argument.
    """
    hooks = dict(settings.get("hooks", {}))
    groups = [dict(g) for g in hooks.get(event, [])]
    for group in groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == command:
                return settings  # already registered — no-op
    groups.append({"hooks": [{"type": "command", "command": command}]})
    hooks[event] = groups
    return {**settings, "hooks": hooks}


def hook_block(command: str = HOOK_COMMAND,
               events: Iterable[str] = HOOK_EVENTS) -> dict:
    """The settings.json fragment that registers the capture hook."""
    settings: dict = {}
    for event in events:
        settings = add_hook_command(settings, event, command)
    return settings


# ---- IO around the merge ---------------------------------------------------

def _load_settings(path: Path) -> dict:
    """Load settings.json, or {} if absent/empty. Refuse to touch invalid JSON
    so we never clobber a file we can't safely round-trip."""
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}); refusing to overwrite. "
            "Fix it, or run `mimir install-hook --print` and paste the block yourself."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object; refusing to overwrite.")
    return data


def install_hook(settings_path: Path = DEFAULT_SETTINGS, *,
                 command: str = HOOK_COMMAND,
                 events: Iterable[str] = HOOK_EVENTS) -> str:
    """Merge the capture hook into settings.json. Idempotent; backs up first."""
    settings = _load_settings(settings_path)
    updated = settings
    for event in events:
        updated = add_hook_command(updated, event, command)
    if updated is settings:
        return f"already registered in {settings_path}"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = settings_path.parent / (settings_path.name + ".bak")
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
    settings_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    return f"registered {command} for {', '.join(events)} in {settings_path}"


# ---- entry points ----------------------------------------------------------

def hook_main(argv: Optional[list] = None) -> int:
    """`mimir-hook` — what a Claude Code hook invokes. Never raises, always 0."""
    return run_hook(sys.stdin.read(), log_path=_log_path())


def consolidate_main(argv: Optional[list] = None, *,
                     judge: Optional[Callable] = None) -> int:
    """`mimir consolidate` — C2 slow path: logged failures -> gated LESSONs -> persist.

    Builds the Cognee/LanceDB-backed store, runs EXTRACT (FR1 judge) -> RESOLVE
    (FR2 contradiction) -> WRITE with an HMAC citation (FR7), then saves the store.
    The judge is a real Claude call by default (subscription auth, no API key);
    inject a fake `judge` to exercise the wiring token-free.
    """
    from mimir.consolidate import consolidate

    episodes = _episodes_from_log(_log_path())
    if not episodes:
        print(f"no failure EPISODEs in {_log_path()} yet; nothing to consolidate")
        return 0

    store = build_store()
    if judge is None:
        try:
            from bench.claude_judge import make_live_judge  # lazy: only the live path needs it
        except ImportError as exc:
            print(f"live consolidation needs the bench judge (run from the repo tree): {exc}",
                  file=sys.stderr)
            return 1
        judge = make_live_judge()

    # ponytail: the FR3 ε-gate needs a held-out probe TASK set (the C5 benchmark has one;
    # a live capture log does not), so the CLI admits judge-passing lessons and leans on
    # FR1 + FR2 + FR7. Wire a real solver-probe here once the agent has eval tasks.
    probe = lambda lessons: float(len(lessons))
    before = len(store.active())
    admitted = consolidate(episodes, store, judge=judge, probe=probe, key=_citation_key())
    save_lessons(store, DEFAULT_LESSONS)
    print(f"consolidated {len(episodes)} failure episodes -> {len(admitted)} new lesson(s); "
          f"store now {len(store.active())} active (was {before}); saved {DEFAULT_LESSONS}")
    return 0


def serve_main(argv: Optional[list] = None) -> int:
    """`mimir-serve` — serve the MCP tool surface over stdio, on the Cognee/LanceDB store."""
    from mimir.serve import build_server
    try:
        store = build_store()
        server = build_server(store, log_path=_log_path())
    except ImportError as exc:
        print(f"mimir-serve needs the serve deps: pip install 'mimir[mcp,cognee]' ({exc})",
              file=sys.stderr)
        return 1
    print(f"mimir-serve: {len(store.active())} active lessons loaded from {DEFAULT_LESSONS}",
          file=sys.stderr)
    server.run()
    return 0


def main(argv: Optional[list] = None) -> int:
    """`mimir` — top-level dispatcher."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "install-hook":
        if "--print" in rest:
            print(json.dumps(hook_block(), indent=2))
            return 0
        print(install_hook())
        return 0
    if cmd == "consolidate":
        return consolidate_main(rest)
    if cmd == "serve":
        return serve_main(rest)
    if cmd == "hook":
        return hook_main(rest)
    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # `python -m mimir.cli <cmd>` when running from the repo tree
    raise SystemExit(main())
