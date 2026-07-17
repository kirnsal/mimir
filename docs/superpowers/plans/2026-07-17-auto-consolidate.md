# Auto-consolidate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mimir consolidate` run automatically in the background — threshold+cooldown gated, on by default — so a user never has to remember to run it, matching the spec at `docs/superpowers/specs/2026-07-17-auto-consolidate-design.md`.

**Architecture:** `capture()` increments a tiny O(1) failure counter on every FAIL episode (no log rescanning). Every `mimir-hook`/`mimir-hook-cline` invocation then does a cheap gate check against that counter; when due, it spawns a detached background process (`mimir.cli _auto-consolidate-worker`) that runs the existing `consolidate_main()` unchanged and updates the gate's state on completion.

**Tech Stack:** Python stdlib only for the new module (`json`, `os`, `subprocess`, `time`, `datetime`, `pathlib`) — no new dependencies.

## Global Constraints

- Python >= 3.10 (existing `pyproject.toml` floor).
- New/touched code in `mimir/auto_consolidate.py` and its call sites in `mimir/capture.py` / `mimir/cli.py`'s hook entrypoints must stay dependency-free (no `lancedb`/`mcp`/`bench` imports) — those stay lazy-imported inside `consolidate_main`/`build_store` as today.
- Nothing on the hook-invoked path may raise into the agent loop. `bump_failure_count` and `maybe_trigger` each wrap their entire body in try/except, logging via the stdlib `logging` module (never printing to stdout/stderr), matching `mimir/capture.py`'s existing `run_hook` contract.
- Default-path parameters must be **late-bound**: `def f(x_path: Optional[Path] = None): path = x_path or DEFAULT_X` — never `def f(x_path: Path = DEFAULT_X)`. The codebase's existing tests rely on `monkeypatch.setattr(module, "DEFAULT_X", tmp_path / ...)`, which only works if the default is resolved inside the function body, not baked into the signature at import time (see `build_store`'s `lessons_path = lessons_path or DEFAULT_LESSONS` in `mimir/cli.py` for the existing pattern).
- Test conventions: plain `pytest`, `tmp_path` for isolated paths, `monkeypatch.setattr`/`monkeypatch.setenv`, no real subprocesses spawned in tests (inject a fake `Popen`), no new test frameworks/fixtures.
- Approved defaults (do not change without re-opening the spec): threshold = 5 new failure episodes, cooldown = 4 hours, lock staleness ceiling = 2 hours.

---

### Task 1: State file plumbing + `bump_failure_count`

**Files:**
- Create: `mimir/auto_consolidate.py`
- Test: `tests/test_auto_consolidate.py`

**Interfaces:**
- Produces: `DEFAULT_STATE: Path`, `bump_failure_count(state_path: Optional[Path] = None) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_consolidate.py`:

```python
"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

See docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
import json

from mimir import auto_consolidate as ac


def test_bump_failure_count_creates_state_file_starting_at_one(tmp_path):
    state_path = tmp_path / "state.json"
    ac.bump_failure_count(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["failure_count_total"] == 1


def test_bump_failure_count_accumulates_across_calls(tmp_path):
    state_path = tmp_path / "state.json"
    ac.bump_failure_count(state_path)
    ac.bump_failure_count(state_path)
    ac.bump_failure_count(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["failure_count_total"] == 3


def test_bump_failure_count_never_raises_on_unwritable_path(tmp_path):
    bad = tmp_path  # a directory, not a file -- the write must fail internally
    ac.bump_failure_count(bad)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_consolidate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mimir.auto_consolidate'`

- [ ] **Step 3: Write minimal implementation**

Create `mimir/auto_consolidate.py`:

```python
"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

Keeps the "should we consolidate now" check O(1) regardless of episode-log size:
capture() bumps a small integer counter on every FAIL episode instead of this module
ever re-scanning episodes.jsonl. See
docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
from __future__ import annotations

import json
import logging
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_consolidate.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir/auto_consolidate.py tests/test_auto_consolidate.py
git commit -m "feat: add auto_consolidate failure counter (bump_failure_count)"
```

---

### Task 2: `is_due()` threshold + cooldown gate

**Files:**
- Modify: `mimir/auto_consolidate.py`
- Test: `tests/test_auto_consolidate.py`

**Interfaces:**
- Consumes: `DEFAULT_STATE`, `_read_state` (Task 1)
- Produces: `is_due(state_path: Optional[Path] = None, *, threshold: int, cooldown_hours: float) -> bool`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auto_consolidate.py`:

```python
from datetime import datetime, timedelta, timezone


def _write_state_file(path, **fields):
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_is_due_false_below_threshold(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state_file(state_path, failure_count_total=3, failure_count_at_last_run=0)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is False


def test_is_due_true_on_first_ever_check_once_threshold_met(tmp_path):
    state_path = tmp_path / "state.json"  # no last_run_ts -- never run before
    _write_state_file(state_path, failure_count_total=5, failure_count_at_last_run=0)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is True


def test_is_due_false_when_cooldown_not_elapsed(tmp_path):
    state_path = tmp_path / "state.json"
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _write_state_file(state_path, failure_count_total=10, failure_count_at_last_run=0,
                      last_run_ts=recent)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is False


def test_is_due_true_when_threshold_met_and_cooldown_elapsed(tmp_path):
    state_path = tmp_path / "state.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    _write_state_file(state_path, failure_count_total=10, failure_count_at_last_run=0,
                      last_run_ts=old)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_consolidate.py -v -k is_due`
Expected: FAIL with `AttributeError: module 'mimir.auto_consolidate' has no attribute 'is_due'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/auto_consolidate.py`, change the `datetime` import and add `is_due`:

```python
from datetime import datetime, timedelta, timezone
```

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_consolidate.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir/auto_consolidate.py tests/test_auto_consolidate.py
git commit -m "feat: add auto_consolidate threshold+cooldown gate (is_due)"
```

---

### Task 3: Lock acquire / stale-reclaim

**Files:**
- Modify: `mimir/auto_consolidate.py`
- Test: `tests/test_auto_consolidate.py`

**Interfaces:**
- Consumes: `DEFAULT_LOCK`, `LOCK_STALE_HOURS` (Task 1)
- Produces: `_acquire_lock(lock_path: Optional[Path] = None) -> bool`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auto_consolidate.py`:

```python
import os
import time


def test_acquire_lock_succeeds_when_absent(tmp_path):
    lock_path = tmp_path / "lock"
    assert ac._acquire_lock(lock_path) is True
    assert lock_path.exists()


def test_acquire_lock_fails_when_fresh_lock_exists(tmp_path):
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    assert ac._acquire_lock(lock_path) is False


def test_acquire_lock_reclaims_stale_lock(tmp_path):
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    stale_time = time.time() - (ac.LOCK_STALE_HOURS * 3600 + 60)
    os.utime(lock_path, (stale_time, stale_time))
    assert ac._acquire_lock(lock_path) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_consolidate.py -v -k acquire_lock`
Expected: FAIL with `AttributeError: module 'mimir.auto_consolidate' has no attribute '_acquire_lock'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/auto_consolidate.py`, add near the top:

```python
import os
import time
```

Add the functions:

```python
def _lock_is_stale(lock_path: Path) -> bool:
    age_seconds = time.time() - lock_path.stat().st_mtime
    return age_seconds >= LOCK_STALE_HOURS * 3600


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
        if _lock_is_stale(path):
            path.unlink(missing_ok=True)
            return _acquire_lock(path)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_consolidate.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir/auto_consolidate.py tests/test_auto_consolidate.py
git commit -m "feat: add auto_consolidate lock acquire/stale-reclaim"
```

---

### Task 4: `maybe_trigger()` orchestration

**Files:**
- Modify: `mimir/auto_consolidate.py`
- Test: `tests/test_auto_consolidate.py`

**Interfaces:**
- Consumes: `is_due`, `_acquire_lock`, `DEFAULT_STATE`/`DEFAULT_LOCK`/`DEFAULT_WORKER_LOG`, `ENABLED_ENV`/`THRESHOLD_ENV`/`COOLDOWN_ENV`/`DEFAULT_THRESHOLD`/`DEFAULT_COOLDOWN_HOURS` (Tasks 1-3)
- Produces: `maybe_trigger(log_path: Path, *, state_path=None, lock_path=None, worker_log_path=None, popen=subprocess.Popen) -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auto_consolidate.py`:

```python
import sys


class _FakePopen:
    calls = []

    def __init__(self, args, **kwargs):
        _FakePopen.calls.append((args, kwargs))


def test_maybe_trigger_spawns_worker_when_due_and_enabled(tmp_path, monkeypatch):
    _FakePopen.calls = []
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "lock"
    worker_log = tmp_path / "worker.log"
    _write_state_file(state_path, failure_count_total=5, failure_count_at_last_run=0)
    monkeypatch.delenv(ac.ENABLED_ENV, raising=False)

    ac.maybe_trigger(tmp_path / "episodes.jsonl", state_path=state_path,
                     lock_path=lock_path, worker_log_path=worker_log, popen=_FakePopen)

    assert len(_FakePopen.calls) == 1
    args, kwargs = _FakePopen.calls[0]
    assert args == [sys.executable, "-m", "mimir.cli", "_auto-consolidate-worker"]
    assert lock_path.exists()  # lock taken before spawn


def test_maybe_trigger_does_nothing_when_disabled(tmp_path, monkeypatch):
    _FakePopen.calls = []
    state_path = tmp_path / "state.json"
    _write_state_file(state_path, failure_count_total=5, failure_count_at_last_run=0)
    monkeypatch.setenv(ac.ENABLED_ENV, "0")

    ac.maybe_trigger(tmp_path / "episodes.jsonl", state_path=state_path,
                     lock_path=tmp_path / "lock", worker_log_path=tmp_path / "worker.log",
                     popen=_FakePopen)

    assert _FakePopen.calls == []


def test_maybe_trigger_does_nothing_when_not_due(tmp_path, monkeypatch):
    _FakePopen.calls = []
    state_path = tmp_path / "state.json"
    _write_state_file(state_path, failure_count_total=2, failure_count_at_last_run=0)
    monkeypatch.delenv(ac.ENABLED_ENV, raising=False)

    ac.maybe_trigger(tmp_path / "episodes.jsonl", state_path=state_path,
                     lock_path=tmp_path / "lock", worker_log_path=tmp_path / "worker.log",
                     popen=_FakePopen)

    assert _FakePopen.calls == []


def test_maybe_trigger_does_nothing_when_lock_already_held(tmp_path, monkeypatch):
    _FakePopen.calls = []
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")  # fresh lock: a run is in flight
    _write_state_file(state_path, failure_count_total=5, failure_count_at_last_run=0)
    monkeypatch.delenv(ac.ENABLED_ENV, raising=False)

    ac.maybe_trigger(tmp_path / "episodes.jsonl", state_path=state_path,
                     lock_path=lock_path, worker_log_path=tmp_path / "worker.log",
                     popen=_FakePopen)

    assert _FakePopen.calls == []


def test_maybe_trigger_never_raises_even_if_popen_blows_up(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    _write_state_file(state_path, failure_count_total=5, failure_count_at_last_run=0)
    monkeypatch.delenv(ac.ENABLED_ENV, raising=False)

    def _boom(*a, **k):
        raise OSError("no python on PATH")

    ac.maybe_trigger(tmp_path / "episodes.jsonl", state_path=state_path,
                     lock_path=tmp_path / "lock", worker_log_path=tmp_path / "worker.log",
                     popen=_boom)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_consolidate.py -v -k maybe_trigger`
Expected: FAIL with `AttributeError: module 'mimir.auto_consolidate' has no attribute 'maybe_trigger'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/auto_consolidate.py`, add near the top:

```python
import subprocess
import sys
```

Add the function:

```python
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
```

`os` is already imported (Task 3).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_consolidate.py -v`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir/auto_consolidate.py tests/test_auto_consolidate.py
git commit -m "feat: add auto_consolidate.maybe_trigger background spawn"
```

---

### Task 5: `finish_run()` worker-side cleanup

**Files:**
- Modify: `mimir/auto_consolidate.py`
- Test: `tests/test_auto_consolidate.py`

**Interfaces:**
- Consumes: `_read_state`, `_write_state`, `DEFAULT_STATE`, `DEFAULT_LOCK` (Task 1)
- Produces: `finish_run(state_path: Optional[Path] = None, lock_path: Optional[Path] = None) -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auto_consolidate.py`:

```python
def test_finish_run_updates_state_and_releases_lock(tmp_path):
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    _write_state_file(state_path, failure_count_total=7, failure_count_at_last_run=2)

    ac.finish_run(state_path, lock_path)

    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["failure_count_at_last_run"] == 7
    assert "last_run_ts" in data
    assert not lock_path.exists()


def test_finish_run_releases_lock_even_if_state_write_fails(tmp_path):
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    bad_state_path = tmp_path  # a directory -- the write must fail internally

    ac.finish_run(bad_state_path, lock_path)  # must not raise

    assert not lock_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_consolidate.py -v -k finish_run`
Expected: FAIL with `AttributeError: module 'mimir.auto_consolidate' has no attribute 'finish_run'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/auto_consolidate.py`, add:

```python
def finish_run(state_path: Optional[Path] = None, lock_path: Optional[Path] = None) -> None:
    """Called by the worker when a run completes, success or failure."""
    path = state_path or DEFAULT_STATE
    lpath = lock_path or DEFAULT_LOCK
    try:
        state = _read_state(path)
        state["failure_count_at_last_run"] = state.get("failure_count_total", 0)
        state["last_run_ts"] = datetime.now(timezone.utc).isoformat()
        _write_state(path, state)
    except Exception:
        log.exception("mimir auto_consolidate failed to update state after run")
    finally:
        Path(lpath).unlink(missing_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_consolidate.py -v`
Expected: PASS (17 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir/auto_consolidate.py tests/test_auto_consolidate.py
git commit -m "feat: add auto_consolidate.finish_run worker cleanup"
```

---

### Task 6: Wire `capture()` to bump the counter on FAIL

**Files:**
- Modify: `mimir/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `bump_failure_count` (Task 1)
- Produces: `capture(episode: Episode, *, log_path: Path, state_path: Optional[Path] = None) -> Optional[str]` (new `state_path` kwarg, default `None`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_capture.py`:

```python
from mimir.auto_consolidate import _read_state


def test_capture_bumps_failure_counter_on_fail_episode(tmp_path):
    log = tmp_path / "episodes.jsonl"
    state_path = tmp_path / "state.json"
    capture(_episode(outcome_score=0.0), log_path=log, state_path=state_path)
    assert _read_state(state_path)["failure_count_total"] == 1


def test_capture_does_not_bump_counter_on_pass_episode(tmp_path):
    log = tmp_path / "episodes.jsonl"
    state_path = tmp_path / "state.json"
    capture(_episode(outcome_score=1.0), log_path=log, state_path=state_path)
    assert not state_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capture.py -v -k bump`
Expected: FAIL with `TypeError: capture() got an unexpected keyword argument 'state_path'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/capture.py`, add the import near the top (after `from mimir.models import Episode`):

```python
from mimir.auto_consolidate import bump_failure_count
```

Change the `capture` signature and body:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capture.py -v`
Expected: PASS (all tests in the file, including the 2 new ones)

- [ ] **Step 5: Commit**

```bash
git add mimir/capture.py tests/test_capture.py
git commit -m "feat: bump auto_consolidate failure counter from capture() on FAIL episodes"
```

---

### Task 7: Wire `hook_main` / `hook_main_cline` to call `maybe_trigger`

**Files:**
- Modify: `mimir/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `auto_consolidate.maybe_trigger` (Task 4)
- Produces: `hook_main`/`hook_main_cline` unchanged signatures, now call `auto_consolidate.maybe_trigger(_log_path())` before returning

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
import io


def test_hook_main_calls_auto_consolidate_maybe_trigger(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger",
                        lambda log_path: calls.append(log_path))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.hook_main() == 0
    assert calls == [cli._log_path()]


def test_hook_main_cline_calls_auto_consolidate_maybe_trigger(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger",
                        lambda log_path: calls.append(log_path))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.hook_main_cline() == 0
    assert calls == [cli._log_path()]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v -k maybe_trigger`
Expected: FAIL with `AttributeError: module 'mimir.cli' has no attribute 'auto_consolidate'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/cli.py`, add the import (after `from mimir.capture import ...`):

```python
from mimir import auto_consolidate
```

Change `hook_main` and `hook_main_cline`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (all tests in the file, including the 2 new ones)

- [ ] **Step 5: Commit**

```bash
git add mimir/cli.py tests/test_cli.py
git commit -m "feat: trigger auto-consolidate check after every hook invocation"
```

---

### Task 8: Worker entrypoint + CLI dispatch

**Files:**
- Modify: `mimir/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `consolidate_main` (existing), `auto_consolidate.finish_run` (Task 5)
- Produces: `_auto_consolidate_worker_main(argv: Optional[list] = None) -> int`; `main()` dispatches `"_auto-consolidate-worker"` to it

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_auto_consolidate_worker_main_calls_consolidate_then_finish_run(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "consolidate_main", lambda *a, **k: calls.append("consolidate") or 0)
    monkeypatch.setattr(cli.auto_consolidate, "finish_run", lambda: calls.append("finish"))
    assert cli._auto_consolidate_worker_main() == 0
    assert calls == ["consolidate", "finish"]


def test_auto_consolidate_worker_main_still_finishes_when_consolidate_raises(monkeypatch):
    import mimir.cli as cli

    calls = []

    def _boom(*a, **k):
        raise RuntimeError("judge unreachable")

    monkeypatch.setattr(cli, "consolidate_main", _boom)
    monkeypatch.setattr(cli.auto_consolidate, "finish_run", lambda: calls.append("finish"))
    try:
        cli._auto_consolidate_worker_main()
        assert False, "the worker's own crash should propagate to its process exit code"
    except RuntimeError:
        pass
    assert calls == ["finish"]  # cleanup still ran despite the crash


def test_main_dispatches_auto_consolidate_worker_command(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "_auto_consolidate_worker_main", lambda: calls.append(True) or 0)
    assert cli.main(["_auto-consolidate-worker"]) == 0
    assert calls == [True]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v -k auto_consolidate_worker`
Expected: FAIL with `AttributeError: module 'mimir.cli' has no attribute '_auto_consolidate_worker_main'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/cli.py`, add the function (near `consolidate_main`):

```python
def _auto_consolidate_worker_main(argv: Optional[list] = None) -> int:
    """Spawned by auto_consolidate.maybe_trigger as a detached background process; not a
    user-facing command (deliberately absent from the module docstring/usage text). Runs
    the same consolidate_main() as `mimir consolidate`, then always updates the
    auto-trigger state and releases the lock, even if consolidation itself raised."""
    try:
        return consolidate_main()
    finally:
        auto_consolidate.finish_run()
```

In `main()`, add a dispatch branch (alongside the other `if cmd == ...` checks, before the final `unknown command` fallback):

```python
    if cmd == "_auto-consolidate-worker":
        return _auto_consolidate_worker_main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (all tests in the file, including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add mimir/cli.py tests/test_cli.py
git commit -m "feat: add _auto-consolidate-worker CLI entrypoint"
```

---

### Task 9: Full test suite + README update

**Files:**
- Modify: `README.md`

**Interfaces:**
- None (docs only; no new public interface)

- [ ] **Step 1: Run the full suite to confirm nothing else broke**

Run: `pytest`
Expected: all tests pass (existing suite + the new/modified tests from Tasks 1-8)

- [ ] **Step 2: Update the Quickstart step-3 comment**

In `README.md`, find:

```
# 3. Consolidate: distill logged failures into gated, signed lessons
mimir consolidate
```

Replace with:

```
# 3. Consolidate: happens automatically in the background once enough
#    failures pile up (see "day to day" below) -- or run it yourself:
mimir consolidate
```

- [ ] **Step 3: Update the "What this looks like day to day" section**

In `README.md`, find:

```
## What this looks like day to day

- **You install it once.** The hook logs failures in the background; you
  never think about it again during a normal session.
- **You run `mimir consolidate` when you want the last batch of failures
  turned into lessons** — after a rough session, at the end of the day,
  or on a cron job. It's a deliberate step, not a black box.
- **From then on, recall is automatic.** Every session after that, the
  agent pulls in whatever lessons actually clear the bar for the context
  it's in — you don't ask for it, you just notice fewer repeat mistakes.
- **You can always audit why.** Every lesson traces back to the specific
  failure and the benchmark that proved it helped — `mimir.forget` retires
  one instantly if it ever stops earning its keep.
```

Replace with:

```
## What this looks like day to day

- **You install it once.** The hook logs failures in the background; you
  never think about it again during a normal session.
- **Consolidation happens on its own.** Once enough new failures pile up
  (5 by default) and enough time has passed since the last run (4 hours
  by default), the next hook call quietly spawns a background
  `mimir consolidate` for you — no command to remember. Run
  `mimir consolidate` yourself any time for an on-demand pass, or set
  `MIMIR_AUTO_CONSOLIDATE=0` to go back to fully manual.
- **From then on, recall is automatic.** Every session after that, the
  agent pulls in whatever lessons actually clear the bar for the context
  it's in — you don't ask for it, you just notice fewer repeat mistakes.
- **You can always audit why.** Every lesson traces back to the specific
  failure and the benchmark that proved it helped, and every
  auto-consolidate run is logged to `~/.mimir/auto_consolidate.log` —
  `mimir.forget` retires a lesson instantly if it ever stops earning its
  keep.
```

- [ ] **Step 4: Verify the stale claim is gone**

Run: `grep -n "deliberate step" README.md`
Expected: no output (the line was replaced)

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document silent auto-consolidate default and its escape hatch"
```
