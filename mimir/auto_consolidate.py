"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

Keeps the "should we consolidate now" check O(1) regardless of episode-log size:
capture() bumps a small integer counter on every FAIL episode instead of this module
ever re-scanning episodes.jsonl. See
docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("mimir.auto_consolidate")

DEFAULT_STATE = Path.home() / ".mimir" / "auto_consolidate_state.json"
DEFAULT_LOCK = Path.home() / ".mimir" / "auto_consolidate.lock"
DEFAULT_WORKER_LOG = Path.home() / ".mimir" / "auto_consolidate.log"

ENABLED_ENV = "MIMIR_AUTO_CONSOLIDATE"
THRESHOLD_ENV = "MIMIR_AUTO_CONSOLIDATE_THRESHOLD"
COOLDOWN_ENV = "MIMIR_AUTO_CONSOLIDATE_COOLDOWN_HOURS"
DEFAULT_THRESHOLD = 5
DEFAULT_COOLDOWN_HOURS = 4.0
LOCK_STALE_HOURS = 2.0


def _read_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


def bump_failure_count(state_path: Optional[Path] = None) -> None:
    """Called by capture() on every FAIL episode. Never raises."""
    path = state_path or DEFAULT_STATE
    try:
        state = _read_state(path)
        state["failure_count_total"] = state.get("failure_count_total", 0) + 1
        _write_state(path, state)
    except Exception:
        log.exception("mimir auto_consolidate failed to bump failure counter (non-fatal)")


def is_due(state_path: Optional[Path] = None, *, threshold: int, cooldown_hours: float) -> bool:
    path = state_path or DEFAULT_STATE
    state = _read_state(path)
    total = state.get("failure_count_total", 0)
    at_last_run = state.get("failure_count_at_last_run", 0)
    if total - at_last_run < threshold:
        return False
    last_run_ts = state.get("last_run_ts")
    if last_run_ts is None:
        return True
    last_run = datetime.fromisoformat(last_run_ts)
    return datetime.now(timezone.utc) - last_run >= timedelta(hours=cooldown_hours)


def _stat_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _lock_is_stale(lock_path: Path, *, mtime: Optional[float] = None) -> bool:
    m = mtime if mtime is not None else _stat_mtime(lock_path)
    if m is None:
        return False
    return time.time() - m >= LOCK_STALE_HOURS * 3600


def _acquire_lock(lock_path: Optional[Path] = None) -> bool:
    """Atomically create the lock file. True if acquired; False if a fresh lock
    already exists (a run is in flight). Reclaims a stale lock."""
    path = lock_path or DEFAULT_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        observed_mtime = _stat_mtime(path)
        if observed_mtime is None:
            return _acquire_lock(path)  # vanished between open() and stat(); retry
        if not _lock_is_stale(path, mtime=observed_mtime):
            return False  # fresh lock: a run is genuinely in flight
        # Re-check immediately before unlinking: if another process already
        # reclaimed this lock (mtime moved since we judged it stale), back off
        # instead of deleting its fresh lock. Narrows the reclaim TOCTOU window
        # to this final stat vs. the unlink call; true elimination needs
        # platform-specific locking (flock/msvcrt), not justified for a
        # single-developer local background trigger.
        if _stat_mtime(path) != observed_mtime:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # already gone; fall through to retry the create
        return _acquire_lock(path)


def maybe_trigger(log_path: Path, *, state_path: Optional[Path] = None,
                  lock_path: Optional[Path] = None, worker_log_path: Optional[Path] = None,
                  popen=subprocess.Popen) -> None:
    """Called after every hook invocation. Never raises -- matches run_hook's contract."""
    try:
        if os.environ.get(ENABLED_ENV, "1") == "0":
            return
        threshold = int(os.environ.get(THRESHOLD_ENV, DEFAULT_THRESHOLD))
        cooldown_hours = float(os.environ.get(COOLDOWN_ENV, DEFAULT_COOLDOWN_HOURS))
        if not is_due(state_path, threshold=threshold, cooldown_hours=cooldown_hours):
            return
        if not _acquire_lock(lock_path):
            return
        worker_log = worker_log_path or DEFAULT_WORKER_LOG
        worker_log.parent.mkdir(parents=True, exist_ok=True)
        with open(worker_log, "a", encoding="utf-8") as fh:
            kwargs = dict(stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                kwargs["start_new_session"] = True
            popen([sys.executable, "-m", "mimir.cli", "_auto-consolidate-worker"], **kwargs)
    except Exception:
        log.exception("mimir auto_consolidate failed to trigger (non-fatal)")


def finish_run(state_path: Optional[Path] = None, lock_path: Optional[Path] = None, *,
               advance_baseline: bool = True) -> None:
    """Called by the worker when a run completes, success or failure. Always updates
    last_run_ts (so a persistently-failing environment backs off via cooldown instead of
    retrying on every hook call) and always releases the lock. Only advances
    failure_count_at_last_run when advance_baseline is True -- if consolidate_main
    couldn't even start real work (e.g. missing bench dependency outside the repo tree),
    leaving the baseline behind means the next eligible retry (after cooldown) tries
    again instead of silently forgetting the accumulated failures forever."""
    path = state_path or DEFAULT_STATE
    lpath = lock_path or DEFAULT_LOCK
    try:
        state = _read_state(path)
        if advance_baseline:
            state["failure_count_at_last_run"] = state.get("failure_count_total", 0)
        state["last_run_ts"] = datetime.now(timezone.utc).isoformat()
        _write_state(path, state)
    except Exception:
        log.exception("mimir auto_consolidate failed to update state after run")
    finally:
        Path(lpath).unlink(missing_ok=True)
