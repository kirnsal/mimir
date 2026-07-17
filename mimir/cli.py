"""Mimir CLI — the zero-hustle entry points (PRD G5).

Installed by `pip install mimir`:

    mimir-hook                   # the command a Claude Code hook calls (reads
                                 #   stdin, appends one EPISODE, always exits 0)
    mimir consolidate            # slow path (C2): turn logged failure EPISODEs into
                                 #   gated LESSONs and persist them (needs a live judge)
    mimir-serve                  # serve the MCP tool surface over stdio, backed by
                                 #   the LanceDB LESSON store (pip install 'mimir[mcp]')
    mimir export --digest        # print active lessons as a markdown digest to stdout
    mimir install-hook           # register mimir-hook into ~/.claude/settings.json
                                 #   (idempotent, backs up the old file)
    mimir install-hook --print   # just print the settings block to paste yourself
    mimir install-hook --cline   # write the PostToolUse hook script Cline picks up
                                 #   (capture only; ~/Documents/Cline/Rules/Hooks/)

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

from mimir import auto_consolidate
from mimir.capture import OUTCOME_FAIL, from_cline_hook, run_hook
from mimir.models import Episode, Lesson

DEFAULT_HOME = Path.home() / ".mimir"
DEFAULT_LOG = DEFAULT_HOME / "episodes.jsonl"
DEFAULT_LESSONS = DEFAULT_HOME / "lessons.json"     # persisted LESSON objects (source of truth)
DEFAULT_LANCE = DEFAULT_HOME / "lance.db"           # LanceDB vector index (rebuilt from lessons)
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_COMMAND = "mimir-hook"
HOOK_EVENTS = ("PostToolUse", "SessionEnd")
CITATION_KEY_ENV = "MIMIR_CITATION_KEY"
EMBED_MODEL_ENV = "MIMIR_EMBED_MODEL"   # opt-in real semantic embedder (fastembed model name)

# Cline has no settings.json to merge into — it picks up an executable script named after
# the hook event from this directory (global scope; see docs.cline.bot/features/hooks).
DEFAULT_CLINE_HOOKS_DIR = Path.home() / "Documents" / "Cline" / "Rules" / "Hooks"
CLINE_HOOK_NAME = "PostToolUse"
CLINE_HOOK_COMMAND = "mimir-hook-cline"


def _log_path() -> Path:
    return Path(os.environ.get("MIMIR_EPISODE_LOG", str(DEFAULT_LOG)))


def _citation_key() -> str:
    return os.environ.get(CITATION_KEY_ENV, "mimir-dev")  # HMAC key for FR7 citations


def _embed_fn():
    """None -> LanceDBVectorIndex's own hash_embed default (zero deps, unchanged
    behaviour). Set MIMIR_EMBED_MODEL (e.g. 'BAAI/bge-small-en-v1.5') to opt into
    real local semantic embeddings via fastembed (pip install 'mimir[embed]')."""
    model_name = os.environ.get(EMBED_MODEL_ENV)
    if not model_name:
        return None
    from mimir.store_cognee import fastembed_embed
    return lambda texts: fastembed_embed(texts, model_name=model_name)


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
    """The served/consolidated store: LanceDB vector engine + persisted lessons."""
    from mimir.store_cognee import CogneeLessonStore, LanceDBVectorIndex

    lance_url = lance_url or DEFAULT_LANCE          # resolved at call time, not frozen at import
    lessons_path = lessons_path or DEFAULT_LESSONS
    embed = _embed_fn()
    index_kwargs = {"embed": embed} if embed is not None else {}
    store = CogneeLessonStore(index=LanceDBVectorIndex(url=str(lance_url), **index_kwargs))
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


def _split_for_probe(episodes: list[Episode]) -> tuple[list[Episode], list[Episode]]:
    """Held-out vs extraction split for the live epsilon-gate probe (FR3): last third
    (minimum 1) held out. Fewer than 2 episodes -> no held-out evidence, extract from
    all (the probe will then fail-closed to 0.0 -- see make_live_counterfactual_probe).
    """
    if len(episodes) < 2:
        return [], episodes
    n_held_out = max(1, len(episodes) // 3)
    return episodes[-n_held_out:], episodes[:-n_held_out]


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


def cline_hook_script(command: str = CLINE_HOOK_COMMAND) -> str:
    """The executable script Cline invokes for PostToolUse (POSIX shell)."""
    return f"#!/usr/bin/env sh\nexec {command}\n"


def install_cline_hook(hooks_dir: Path = DEFAULT_CLINE_HOOKS_DIR, *,
                       command: str = CLINE_HOOK_COMMAND) -> str:
    """Write the PostToolUse hook script Cline picks up automatically. It's mimir's own
    file (nothing to merge, unlike Claude Code's settings.json), so this just overwrites."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script_path = hooks_dir / CLINE_HOOK_NAME
    # newline="\n": the shebang line must stay LF-only even when written on Windows,
    # or a POSIX `sh` fails to resolve "/usr/bin/env sh\r" (bad-interpreter error).
    script_path.write_text(cline_hook_script(command), encoding="utf-8", newline="\n")
    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except OSError:
        pass  # ponytail: no POSIX exec bit on Windows; Cline's Windows invocation is unconfirmed
    return f"wrote {script_path}"


# ---- entry points ----------------------------------------------------------

def _ensure_utf8_stdio() -> None:
    """Windows terminals often default to a legacy codepage (e.g. cp1252) that can't
    encode the em-dashes used throughout Mimir's CLI text, mangling output into
    mojibake on the very first run. Reconfigure once per entry point instead of
    avoiding non-ASCII in every print()."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass  # non-reconfigurable stream (e.g. redirected to a pipe); leave as-is


def hook_main(argv: Optional[list] = None) -> int:
    """`mimir-hook` — what a Claude Code hook invokes. Never raises, always 0."""
    _ensure_utf8_stdio()
    rc = run_hook(sys.stdin.read(), log_path=_log_path())
    auto_consolidate.maybe_trigger(_log_path())
    return rc


def hook_main_cline(argv: Optional[list] = None) -> int:
    """`mimir-hook-cline` — what the Cline PostToolUse hook script invokes."""
    _ensure_utf8_stdio()
    rc = run_hook(sys.stdin.read(), log_path=_log_path(), mapper=from_cline_hook)
    auto_consolidate.maybe_trigger(_log_path())
    return rc


def consolidate_main(argv: Optional[list] = None, *, judge: Optional[Callable] = None,
                     probe: Optional[Callable] = None) -> int:
    """`mimir consolidate` — C2 slow path: logged failures -> gated LESSONs -> persist.

    Builds the Cognee/LanceDB-backed store, runs EXTRACT (FR1 judge) -> ADMIT (FR3
    live counterfactual epsilon-gate) -> RESOLVE (FR2 contradiction) -> WRITE with an
    HMAC citation (FR7), then saves the store. `judge`/`probe` are real Claude calls
    by default (subscription auth, no API key); inject fakes to exercise the wiring
    token-free.
    """
    from mimir.consolidate import consolidate, sweep_episodes

    episodes = _episodes_from_log(_log_path())
    if not episodes:
        print(f"no failure EPISODEs in {_log_path()} yet; nothing to consolidate")
        return 0

    store = build_store()
    total = len(episodes)

    if judge is None:
        try:
            from bench.claude_judge import make_live_judge  # lazy: only the live path needs it
        except ImportError as exc:
            print(f"live consolidation needs the bench judge (run from the repo tree): {exc}",
                  file=sys.stderr)
            return 1
        judge = make_live_judge()

    if probe is None:
        try:
            from bench.claude_judge import make_live_counterfactual_probe  # lazy, same reason
        except ImportError as exc:
            print(f"live consolidation needs the bench probe (run from the repo tree): {exc}",
                  file=sys.stderr)
            return 1
        held_out, episodes = _split_for_probe(episodes)
        probe = make_live_counterfactual_probe(held_out)

    before = len(store.active())
    admitted = consolidate(episodes, store, judge=judge, probe=probe, key=_citation_key())

    # FR4: sweep the full log (not just failures) for lessons whose real-world adoption
    # correlates with regressions -- catches what ADMIT's ε-gate can't (it only sees the
    # probe set at write time, not how the lesson performs once actually used later).
    all_episodes = _episodes_from_log(_log_path(), failures_only=False)
    quarantined = sweep_episodes(store, all_episodes)

    save_lessons(store, DEFAULT_LESSONS)
    print(f"consolidated {total} failure episodes -> {len(admitted)} new lesson(s); "
          f"{len(quarantined)} lesson(s) quarantined by the FR4 circuit breaker; "
          f"store now {len(store.active())} active (was {before}); saved {DEFAULT_LESSONS}")
    return 0


def _auto_consolidate_worker_main(argv: Optional[list] = None) -> int:
    """Spawned by auto_consolidate.maybe_trigger as a detached background process; not a
    user-facing command (deliberately absent from the module docstring/usage text). Runs
    the same consolidate_main() as `mimir consolidate`, then always updates the
    auto-trigger state and releases the lock, even if consolidation itself raised."""
    try:
        return consolidate_main()
    finally:
        auto_consolidate.finish_run()


def serve_main(argv: Optional[list] = None) -> int:
    """`mimir-serve` — serve the MCP tool surface over stdio, on the LanceDB store."""
    _ensure_utf8_stdio()
    from mimir.serve import build_server
    try:
        store = build_store()
        server = build_server(store, log_path=_log_path())
    except ImportError as exc:
        print(f"mimir-serve needs the serve deps: pip install 'mimir[mcp]' ({exc})",
              file=sys.stderr)
        return 1
    print(f"mimir-serve: {len(store.active())} active lessons loaded from {DEFAULT_LESSONS}",
          file=sys.stderr)
    server.run()
    return 0


def render_digest(lessons: list[Lesson]) -> str:
    """Human-readable markdown snapshot of active lessons, sorted by confidence desc."""
    if not lessons:
        return "# Mimir digest\n\nno active lessons yet.\n"
    lines = ["# Mimir digest", ""]
    for lesson in sorted(lessons, key=lambda lo: lo.confidence, reverse=True):
        lines.append(f"- **{lesson.rule}** (confidence: {lesson.confidence:.2f}, id: {lesson.id})")
    return "\n".join(lines) + "\n"


def export_main(argv: Optional[list] = None) -> int:
    """`mimir export --digest` — markdown snapshot of active lessons, printed to stdout.
    Redirect with `>` for a file; no new file-writing path (same stdout convention as
    `install-hook --print`)."""
    argv = argv or []
    if "--digest" not in argv:
        print("usage: mimir export --digest", file=sys.stderr)
        return 2
    try:
        store = build_store()
    except ImportError as exc:
        print(f"mimir export needs the serve deps: pip install 'mimir[mcp]' ({exc})",
              file=sys.stderr)
        return 1
    print(render_digest(store.active()))
    return 0


def main(argv: Optional[list] = None) -> int:
    """`mimir` — top-level dispatcher."""
    _ensure_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "install-hook":
        if "--cline" in rest:
            if "--print" in rest:
                print(cline_hook_script())
            else:
                print(install_cline_hook())
            return 0
        if "--print" in rest:
            print(json.dumps(hook_block(), indent=2))
            return 0
        print(install_hook())
        return 0
    if cmd == "consolidate":
        return consolidate_main(rest)
    if cmd == "serve":
        return serve_main(rest)
    if cmd == "export":
        return export_main(rest)
    if cmd == "hook":
        return hook_main(rest)
    if cmd == "_auto-consolidate-worker":
        return _auto_consolidate_worker_main()
    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # `python -m mimir.cli <cmd>` when running from the repo tree
    raise SystemExit(main())
