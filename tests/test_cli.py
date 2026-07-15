import json

import pytest

from mimir.cli import add_hook_command, cline_hook_script, install_cline_hook, install_hook


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


def test_install_cline_hook_writes_executable_script(tmp_path):
    hooks_dir = tmp_path / "Hooks"
    msg = install_cline_hook(hooks_dir)
    script = hooks_dir / "PostToolUse"
    assert script.exists()
    assert "mimir-hook-cline" in script.read_text(encoding="utf-8")
    assert str(script) in msg

    install_cline_hook(hooks_dir)  # re-running just overwrites; no error, still one file
    assert list(hooks_dir.iterdir()) == [script]


def test_cline_hook_script_execs_given_command():
    assert "exec mimir-hook-cline" in cline_hook_script()
    assert "exec other-cmd" in cline_hook_script("other-cmd")


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
    fake_probe = lambda lessons: float(len(lessons))  # mimic original no-op; live probe wiring covered separately
    assert cli.consolidate_main(judge=fake_judge, probe=fake_probe) == 0
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


def test_split_for_probe_holds_out_last_third_minimum_one():
    import mimir.cli as cli
    from mimir.models import Episode

    eps = [Episode(id=str(i)) for i in range(6)]
    held_out, extraction = cli._split_for_probe(eps)

    assert held_out == eps[-2:]      # 6 // 3 = 2
    assert extraction == eps[:-2]


def test_split_for_probe_extracts_all_when_fewer_than_two_episodes():
    import mimir.cli as cli
    from mimir.models import Episode

    assert cli._split_for_probe([]) == ([], [])
    one = [Episode(id="1")]
    assert cli._split_for_probe(one) == ([], one)


def test_consolidate_main_wires_live_probe_with_held_out_split(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import bench.claude_judge as cj
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="a1", context="c1", consequence="boom1", outcome_score=0.0), log_path=log)
    capture(Episode(action="a2", context="c2", consequence="boom2", outcome_score=0.0), log_path=log)
    capture(Episode(action="a3", context="c3", consequence="boom3", outcome_score=0.0), log_path=log)

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    seen_held_out = []

    def fake_make_probe(held_out, **kw):
        seen_held_out.append(held_out)
        return lambda lessons: float(len(lessons))  # improvement proportional to lessons admitted

    monkeypatch.setattr(cj, "make_live_counterfactual_probe", fake_make_probe)

    fake_judge = lambda ep: Verdict(rule=f"rule for {ep.action}",
                                    specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0

    assert len(seen_held_out) == 1
    held_out = seen_held_out[0]
    assert len(held_out) == 1           # 3 episodes -> 1 held out (last third, min 1)
    assert held_out[0].action == "a3"   # last episode held out, not extracted from


def test_consolidate_main_admits_nothing_with_one_episode_no_probe_injected(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="a1", context="c1", consequence="boom1", outcome_score=0.0), log_path=log)

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    fake_judge = lambda ep: Verdict(rule="a rule", specificity=0.9,
                                    generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0  # still exits 0

    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    assert store.active() == []  # fail-closed: no held-out evidence, nothing admitted


def test_render_digest_sorts_by_confidence_descending():
    import mimir.cli as cli
    from mimir.models import Lesson

    lessons = [Lesson(rule="low confidence rule", confidence=0.3, id="L1"),
              Lesson(rule="high confidence rule", confidence=0.9, id="L2")]
    digest = cli.render_digest(lessons)
    assert digest.index("high confidence rule") < digest.index("low confidence rule")
    assert "confidence: 0.90" in digest
    assert "id: L2" in digest


def test_render_digest_handles_no_active_lessons():
    import mimir.cli as cli

    assert "no active lessons" in cli.render_digest([])


def test_export_main_requires_digest_flag():
    import mimir.cli as cli

    assert cli.export_main([]) == 2


def test_export_main_prints_digest_from_store(tmp_path, monkeypatch, capsys):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.models import Lesson

    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    store = cli.build_store()
    store.add(Lesson(rule="pin tool versions before release", confidence=0.8, id="L1"))
    cli.save_lessons(store, cli.DEFAULT_LESSONS)

    assert cli.export_main(["--digest"]) == 0
    out = capsys.readouterr().out
    assert "pin tool versions before release" in out


def test_main_dispatches_export_command(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "export_main", lambda rest: calls.append(rest) or 0)
    assert cli.main(["export", "--digest"]) == 0
    assert calls == [["--digest"]]
