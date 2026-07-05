"""Zero-token test: MIMIR_CLAUDE_MODEL adds `--model` to the claude invocation."""
from bench import claude_cli


class _FakeProc:
    returncode = 0
    stdout = '{"result": "ok"}'
    stderr = ""


def _capture_run(monkeypatch):
    """Patch subprocess.run to record the args list and return a success envelope."""
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        return _FakeProc()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    return calls


def test_model_env_adds_flag(monkeypatch):
    calls = _capture_run(monkeypatch)
    monkeypatch.setenv("MIMIR_CLAUDE_MODEL", "sonnet")
    out = claude_cli._subprocess_runner("fix this", 10)
    assert out == '{"result": "ok"}'
    assert "--model" in calls["args"]
    assert calls["args"][calls["args"].index("--model") + 1] == "sonnet"


def test_no_model_env_keeps_args_unchanged(monkeypatch):
    calls = _capture_run(monkeypatch)
    monkeypatch.delenv("MIMIR_CLAUDE_MODEL", raising=False)
    claude_cli._subprocess_runner("fix this", 10)
    assert "--model" not in calls["args"]
    assert calls["args"][1:] == ["-p", "--output-format", "json"]


def test_empty_model_env_keeps_args_unchanged(monkeypatch):
    calls = _capture_run(monkeypatch)
    monkeypatch.setenv("MIMIR_CLAUDE_MODEL", "")
    claude_cli._subprocess_runner("fix this", 10)
    assert "--model" not in calls["args"]
