"""Optional MCP transport. Most tests use an injected fake FastMCP (no `mcp` dep);
the real-transport test skips unless the `mcp` SDK is installed.

Behaviour under test:
- build_server registers every handler-bearing tool onto the server
- forget always registers; recall registers when the store has active lessons;
  capture/consolidate register only with a log_path
- attribute (handler=None: needs an injected solver) is skipped
- against the REAL FastMCP: tools list with correct schemas and recall round-trips
"""
import asyncio

import pytest

from mimir.models import Lesson
from mimir.serve import build_server
from mimir.store import InMemoryLessonStore


class _FakeMCP:
    def __init__(self):
        self.registered = []

    def add_tool(self, fn, name=None, description=None):
        self.registered.append(name)


def _store_with_a_lesson():
    store = InMemoryLessonStore()
    store.add(Lesson(id="L1", rule="flush the buffer before reading",
                     confidence=0.9, supporting_episodes=["e1"]))
    return store


def test_build_server_registers_only_handler_bearing_tools(tmp_path):
    srv = build_server(_store_with_a_lesson(),
                       log_path=tmp_path / "ep.jsonl", fastmcp=_FakeMCP())
    assert "mimir.recall" in srv.registered            # live: store has an active lesson
    assert "mimir.forget" in srv.registered            # live: always
    assert "mimir.capture" in srv.registered           # live: log_path given
    assert "mimir.consolidate" in srv.registered       # live: log_path given
    assert "mimir.attribute" not in srv.registered      # needs a solver


def test_build_server_skips_capture_and_consolidate_without_log_path():
    srv = build_server(_store_with_a_lesson(), fastmcp=_FakeMCP())
    assert "mimir.recall" in srv.registered
    assert "mimir.forget" in srv.registered
    assert "mimir.capture" not in srv.registered
    assert "mimir.consolidate" not in srv.registered


def test_build_server_skips_recall_when_store_has_no_active_lessons():
    srv = build_server(InMemoryLessonStore(), fastmcp=_FakeMCP())
    assert "mimir.recall" not in srv.registered
    assert "mimir.forget" in srv.registered  # unaffected by the recall gate


def test_real_fastmcp_transport_round_trips_recall(tmp_path):
    """The real SDK path: a lambda handler returning a RecallResult dataclass must
    actually register and serialize over the transport, not just pass the fake."""
    pytest.importorskip("mcp")

    store = InMemoryLessonStore()
    store.add(Lesson(id="L1", rule="flush the buffer before reading",
                     confidence=0.9, supporting_episodes=["e1"]))
    srv = build_server(store, log_path=tmp_path / "ep.jsonl")

    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert {"mimir.recall", "mimir.capture"} <= names

    result = asyncio.run(srv.call_tool("mimir.recall", {"query": "read is empty, need flush"}))
    # FastMCP returns (content_blocks, structured) or a content list across versions;
    # the gated lesson's rule must survive serialization either way.
    assert "flush the buffer" in str(result)
