import json

import pytest

from mimir.cli import add_hook_command, install_hook


def test_add_hook_command_is_idempotent():
    once = add_hook_command({}, "PostToolUse", "mimir-hook")
    twice = add_hook_command(once, "PostToolUse", "mimir-hook")
    assert twice is once  # second add is a no-op, no duplicate
    groups = once["hooks"]["PostToolUse"]
    n = sum(e["command"] == "mimir-hook" for g in groups for e in g["hooks"])
    assert n == 1


def test_add_hook_command_preserves_existing_and_does_not_mutate():
    existing = {"hooks": {"PostToolUse": [
        {"hooks": [{"type": "command", "command": "other-tool"}]}]}}
    updated = add_hook_command(existing, "PostToolUse", "mimir-hook")
    cmds = [e["command"] for g in updated["hooks"]["PostToolUse"] for e in g["hooks"]]
    assert cmds == ["other-tool", "mimir-hook"]
    # original untouched (immutability)
    assert len(existing["hooks"]["PostToolUse"]) == 1
    assert existing["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "other-tool"


def test_install_hook_writes_backs_up_and_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    msg = install_hook(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"  # unrelated settings preserved
    assert "PostToolUse" in data["hooks"] and "SessionEnd" in data["hooks"]
    assert (tmp_path / "settings.json.bak").exists()  # backup made
    assert "registered" in msg

    msg2 = install_hook(settings)  # second run is a no-op
    assert "already registered" in msg2


def test_install_hook_refuses_invalid_json(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json", encoding="utf-8")
    try:
        install_hook(settings)
        assert False, "should have refused invalid JSON"
    except SystemExit:
        pass


# --- end-to-end: consolidate (log -> gated lessons) -> serve-side store (#1/#2) ---

def test_consolidate_fills_store_and_serve_side_loads_it(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.mcp_server import recall
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="json.loads", context="empty stdin",
                    consequence="JSONDecodeError", outcome_score=0.0, task_id="t"), log_path=log)
    capture(Episode(action="ok", context="c", consequence="fine",
                    outcome_score=1.0), log_path=log)  # a success: not a MISTAKE, must be ignored

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    fake_judge = lambda ep: Verdict(rule="guard json decode against empty input",
                                    specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0
    assert (tmp_path / "lessons.json").exists()

    # a fresh (serve-side) store built from the same paths recalls what consolidate wrote
    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    res = recall(store, "json parser crashes on empty input")
    assert res.lessons and "json" in res.lessons[0].rule


def test_store_persistence_survives_reload_bitemporally(tmp_path):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.models import Lesson

    lessons = tmp_path / "lessons.json"
    s1 = cli.build_store(lance_url=tmp_path / "a.db", lessons_path=lessons)
    old = s1.add(Lesson(rule="retry once", confidence=0.5, id="L1"))
    s1.supersede(old, Lesson(rule="retry with exponential backoff", confidence=0.8, id="L2"))
    cli.save_lessons(s1, lessons)

    s2 = cli.build_store(lance_url=tmp_path / "b.db", lessons_path=lessons)
    assert {lo.id for lo in s2.active()} == {"L2"}        # bi-temporal state survives reload
    assert s2.get("L1").status == "superseded"
