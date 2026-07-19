"""C4 — MCP retrieval server. Tests written first (TDD RED).

Behaviour under test (BUILD_SPEC C4 "Tests write first", PRD FR5):
- recall excludes quarantined / low-confidence / contradicted lessons
- recall raises an UNCERTAINTY flag when retrieved support is thin
- recall ranks active, high-confidence, on-topic lessons and returns them
- the MCP tool surface validates (names + well-formed JSON input schemas)
"""
from mimir.models import Lesson, QUARANTINED
from mimir.store import InMemoryLessonStore
from mimir import mcp_server as M


def _store(*lessons):
    store = InMemoryLessonStore()
    for lo in lessons:
        store.add(lo)
    return store


def _lesson(rule, confidence=0.9, support=("E1",), **kw):
    return Lesson(rule=rule, confidence=confidence,
                  supporting_episodes=list(support), **kw)


# --- recall / FR5 ------------------------------------------------------------

def test_recall_returns_active_on_topic_high_confidence_lesson():
    store = _store(_lesson("use exponential backoff on http 429 retries"))
    res = M.recall(store, "how should I handle 429 retries")
    assert [lo.rule for lo in res.lessons] == ["use exponential backoff on http 429 retries"]
    assert res.uncertain is False


def test_recall_excludes_quarantined_lowconf_and_contradicted():
    quarantined = _lesson("force-push to resolve retry conflicts", status=QUARANTINED)
    low_conf = _lesson("maybe retry the retry sometimes", confidence=0.2)
    winner = _lesson("retry with backoff", confidence=0.9)
    loser = _lesson("retry immediately without backoff", confidence=0.9,
                    id="LOSE")
    # winner contradicts loser; loser is still active but must be excluded from recall
    winner.contradicts = ["LOSE"]
    store = _store(quarantined, low_conf, winner, loser)

    rules = {lo.rule for lo in M.recall(store, "retry backoff").lessons}

    assert "retry with backoff" in rules
    assert "force-push to resolve retry conflicts" not in rules   # quarantined
    assert "maybe retry the retry sometimes" not in rules         # below tau
    assert "retry immediately without backoff" not in rules       # contradicted


def test_recall_flags_uncertainty_when_no_lesson_matches():
    store = _store(_lesson("use backoff on 429"))
    res = M.recall(store, "what colour should the button be")
    assert res.lessons == []
    assert res.uncertain is True


def test_recall_flags_uncertainty_on_thin_supporting_evidence():
    # on-topic, high-confidence, but backed by zero episodes -> thin support
    store = _store(_lesson("prefer composition over inheritance", support=()))
    res = M.recall(store, "composition vs inheritance design")
    assert [lo.rule for lo in res.lessons] == ["prefer composition over inheritance"]
    assert res.uncertain is True


# --- MCP tool surface --------------------------------------------------------

def test_tool_surface_validates_and_recall_is_wired():
    store = _store(_lesson("use backoff on 429"))
    tools = M.build_tools(store)

    # the full lifecycle surface is registered: remember/memify/recall/forget + attribute
    assert {"mimir.recall", "mimir.attribute", "mimir.capture",
            "mimir.consolidate", "mimir.forget"} <= set(tools)

    for tool in tools.values():
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        # every required key is a declared property (well-formed JSON Schema)
        assert set(schema["required"]) <= set(schema["properties"])

    # recall is live; it returns a RecallResult through the tool handler
    res = tools["mimir.recall"].handler(query="429 backoff")
    assert isinstance(res, M.RecallResult)
    assert res.lessons


def test_recall_tool_omitted_when_store_has_no_active_lessons():
    tools = M.build_tools(_store())  # empty store, no active lessons
    assert "mimir.recall" not in tools


def test_recall_tool_present_when_store_has_active_lessons():
    store = _store(_lesson("use backoff on 429"))
    tools = M.build_tools(store)
    assert "mimir.recall" in tools


def test_recall_tool_present_when_active_lessons_check_raises():
    class _BoomStore:
        def active(self):
            raise RuntimeError("store unavailable")

    tools = M.build_tools(_BoomStore())
    assert "mimir.recall" in tools


def test_capture_tool_is_live_when_log_path_given(tmp_path):
    import json
    log = tmp_path / "episodes.jsonl"
    tools = M.build_tools(_store(), log_path=log)
    eid = tools["mimir.capture"].handler(action="ran tests", outcome_score=0.0)
    assert eid
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "ran tests"
    assert row["outcome_score"] == 0.0


def test_capture_tool_declared_only_without_log_path():
    tools = M.build_tools(_store())  # no log destination -> not bound
    assert tools["mimir.capture"].handler is None


# --- memify (consolidate) / forget -------------------------------------------

def _fake_judge(rule="always flush before reading"):
    from mimir.consolidate import Verdict

    def judge(ep):
        return Verdict(rule=rule, specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    return judge


def test_consolidate_tool_declared_only_without_log_path():
    tools = M.build_tools(_store())  # no log -> nothing to read episodes from
    assert tools["mimir.consolidate"].handler is None


def test_consolidate_tool_is_live_with_injected_judge(tmp_path):
    import json

    log = tmp_path / "episodes.jsonl"
    log.write_text(json.dumps({
        "action": "read stale cache", "context": "cache lookup",
        "consequence": "returned expired data", "outcome_score": 0.0,
        "session_id": "s1", "task_id": "t1", "id": "E1",
    }) + "\n", encoding="utf-8")

    store = InMemoryLessonStore()
    tools = M.build_tools(store, log_path=log, consolidate_judge=_fake_judge(),
                          consolidate_probe=lambda lessons: float(len(lessons)))

    result = tools["mimir.consolidate"].handler()

    assert result["admitted"] == 1
    assert result["active_lessons"] == 1
    admitted = store.active()[0]
    assert admitted.rule == "always flush before reading"
    assert admitted.citation  # FR7: HMAC-signed


def test_forget_tool_retires_a_lesson():
    lid = "L1"
    store = _store(_lesson("stale rule", id=lid))
    tools = M.build_tools(store)

    result = tools["mimir.forget"].handler(lesson_id=lid)

    assert result == {"lesson_id": lid, "status": "retired"}
    assert lid not in {lo.id for lo in store.active()}
    assert store.get(lid).status == "retired"
