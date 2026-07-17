"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

See docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

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


def test_acquire_lock_backs_off_if_lock_refreshed_during_reclaim(tmp_path, monkeypatch):
    """Simulates two processes racing to reclaim the same stale lock: if the
    lock's mtime changes between the staleness check and the unlink, back off
    instead of stomping the other process's fresh lock."""
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    stale_time = time.time() - (ac.LOCK_STALE_HOURS * 3600 + 60)
    fresh_time = time.time()

    mtimes = iter([stale_time, fresh_time])
    monkeypatch.setattr(ac, "_stat_mtime", lambda path: next(mtimes))

    assert ac._acquire_lock(lock_path) is False
    assert lock_path.exists()  # not stomped -- still the original file


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


def test_finish_run_updates_timestamp_but_not_baseline_when_advance_baseline_false(tmp_path):
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    _write_state_file(state_path, failure_count_total=7, failure_count_at_last_run=2)

    ac.finish_run(state_path, lock_path, advance_baseline=False)

    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["failure_count_at_last_run"] == 2  # unchanged -- failures stay pending
    assert "last_run_ts" in data  # still updated, so cooldown still backs off retries
    assert not lock_path.exists()  # lock still released
