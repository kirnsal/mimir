"""C1 — fast-path hook listener. Tests first (TDD RED).

Invariants under test (BUILD_SPEC C1):
- capture appends a well-formed EPISODE to the append-only JSONL log
- a failed tool event is scored OUTCOME_FAIL; a successful one OUTCOME_PASS
- the fast path touches no network / LLM (latency budget = O(1) local append)
- capture never raises into the agent loop (swallow-and-log loudly)
"""
import json
import logging
import socket

from mimir.capture import (
    OUTCOME_FAIL,
    OUTCOME_PASS,
    capture,
    from_cline_hook,
    from_hermes_call,
    from_hook,
    run_hook,
)
from mimir.models import Episode


def _episode(**kw):
    base = dict(action="ran tests", context="C1", consequence="green", session_id="s1")
    base.update(kw)
    return Episode(**base)


def test_capture_appends_wellformed_episode(tmp_path):
    log = tmp_path / "episodes.jsonl"
    eid = capture(_episode(), log_path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["id"] == eid
    assert row["action"] == "ran tests"
    assert row["session_id"] == "s1"
    assert row["timestamp"] is not None  # stamped on capture


def test_capture_is_append_only(tmp_path):
    log = tmp_path / "episodes.jsonl"
    capture(_episode(action="first"), log_path=log)
    capture(_episode(action="second"), log_path=log)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_failed_tool_event_scored_as_fail():
    fail = from_hook({"tool_name": "Bash", "is_error": True, "session_id": "s1"})
    ok = from_hook({"tool_name": "Bash", "is_error": False, "session_id": "s1"})
    assert fail.outcome_score == OUTCOME_FAIL
    assert ok.outcome_score == OUTCOME_PASS


def test_fast_path_touches_no_network(tmp_path, monkeypatch):
    # if capture tried to reach an LLM/network, opening a socket would blow up
    def _boom(*a, **k):
        raise AssertionError("fast path must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    eid = capture(_episode(), log_path=tmp_path / "episodes.jsonl")
    assert eid is not None


def test_capture_never_raises_into_agent_loop(tmp_path, caplog):
    # log_path points at a directory -> the write fails, but capture must not raise
    bad = tmp_path  # a directory, not a file
    with caplog.at_level(logging.ERROR):
        result = capture(_episode(), log_path=bad)
    assert result is None
    assert any(r.levelno >= logging.ERROR for r in caplog.records)  # logged loudly


def test_run_hook_captures_event_from_stdin(tmp_path):
    log = tmp_path / "episodes.jsonl"
    event = {"tool_name": "Bash", "tool_input": {"command": "pytest"},
             "is_error": True, "session_id": "s1"}
    rc = run_hook(json.dumps(event), log_path=log)
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "Bash"
    assert row["outcome_score"] == OUTCOME_FAIL


def test_run_hook_swallows_malformed_input(tmp_path, caplog):
    log = tmp_path / "episodes.jsonl"
    with caplog.at_level(logging.ERROR):
        rc = run_hook("not json{", log_path=log)  # never blocks the agent loop
    assert rc == 0
    assert not log.exists()  # nothing captured
    assert any(r.levelno >= logging.ERROR for r in caplog.records)  # logged, not silent


def test_run_hook_ignores_empty_stdin(tmp_path):
    log = tmp_path / "episodes.jsonl"
    assert run_hook("", log_path=log) == 0  # SessionEnd may fire with no payload
    assert not log.exists()


# --- Cline PostToolUse mapper: same contract, a different runtime's payload shape ---

def test_cline_failed_tool_event_scored_as_fail():
    fail = from_cline_hook({"toolName": "execute_command", "taskId": "t1",
                            "tool_response": {"success": False}})
    ok = from_cline_hook({"toolName": "execute_command", "taskId": "t1",
                          "tool_response": {"success": True}})
    assert fail.outcome_score == OUTCOME_FAIL
    assert ok.outcome_score == OUTCOME_PASS


def test_cline_hook_maps_task_id_to_session_and_task():
    ep = from_cline_hook({"toolName": "execute_command", "taskId": "abc123"})
    assert ep.session_id == "abc123"
    assert ep.task_id == "abc123"


def test_run_hook_accepts_a_mapper_for_other_runtimes(tmp_path):
    log = tmp_path / "episodes.jsonl"
    event = {"toolName": "execute_command", "taskId": "t1", "tool_response": {"success": False}}
    rc = run_hook(json.dumps(event), log_path=log, mapper=from_cline_hook)
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "execute_command"
    assert row["outcome_score"] == OUTCOME_FAIL


# --- Hermes post_tool_call(tool_name, params, result) mapper: no dict payload here ---

def test_hermes_call_maps_positional_args_to_episode():
    ep = from_hermes_call("execute_command", {"cmd": "pytest"}, {"output": "ok"})
    assert ep.action == "execute_command"
    assert ep.outcome_score == OUTCOME_PASS


def test_hermes_call_exception_result_scored_as_fail():
    ep = from_hermes_call("execute_command", {"cmd": "pytest"}, RuntimeError("boom"))
    assert ep.outcome_score == OUTCOME_FAIL


def test_hermes_call_error_dict_scored_as_fail():
    ep = from_hermes_call("execute_command", {}, {"error": "boom"})
    assert ep.outcome_score == OUTCOME_FAIL


# --- auto_consolidate: bump failure counter on FAIL episodes ---

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
