# Plugging in a new tool (generic hook adapter)

Mimir ships bespoke mappers for Claude Code, Cline, and Hermes. For
anything else, if your tool's hook mechanism can run an arbitrary shell
command with a JSON payload on stdin — which covers most "hook"
mechanisms — you can wire it up yourself with a small JSON config, no
Mimir code changes needed.

## How it works

1. Write a config file describing where the fields Mimir needs live in
   your tool's own hook payload.
2. Point `mimir hook` at it, either with `--config <path>` or by setting
   `MIMIR_HOOK_CONFIG=<path>` once in your tool's own hook environment.
3. Wire your tool's hook to run `mimir hook --config <path>` (or just
   `mimir hook` if you used the env var), with the payload piped to stdin.

## Config format

```json
{
  "action_path": "tool",
  "context_path": "input",
  "consequence_path": "output",
  "session_id_path": "session.id",
  "task_id_path": "task.id",
  "outcome_path": "output.status",
  "fail_values": ["error", "failed", false]
}
```

Each `*_path` is a dotted path into your tool's JSON payload
(`"output.status"` -> `payload["output"]["status"]`). Missing or
unreachable paths resolve to an empty string for
`action`/`session_id`/`task_id`. The outcome is `FAIL` when the value at
`outcome_path` is a member of `fail_values`; anything else (including an
unresolved path) is `PASS`. `fail_values` must be a JSON array, even for a
single value (`["error"]`, not `"error"`) — a bare string is matched as a
substring test, not an exact match.

**Limitations:** dict traversal only (no `items.0.status` list indexing);
one `outcome_path` plus one `fail_values` list (no combining multiple
fields). If your tool needs either, write a Python mapper instead —
`from_cline_hook` in `mimir/capture.py` is a template.

## Worked example: a tool called "Foo"

Say Foo's hook payload looks like this:

```json
{
  "tool": "foo.run",
  "input": {"target": "build"},
  "output": {"status": "error", "message": "build failed: missing dependency"}
}
```

The config, saved as `~/.mimir/hooks/foo.json`:

```json
{
  "action_path": "tool",
  "context_path": "input",
  "consequence_path": "output",
  "outcome_path": "output.status",
  "fail_values": ["error"]
}
```

Foo's own hook configuration then runs:

```
mimir hook --config ~/.mimir/hooks/foo.json
```

(or set `MIMIR_HOOK_CONFIG=~/.mimir/hooks/foo.json` in Foo's hook
environment and just run `mimir hook`.)

Every hook firing appends one EPISODE to `~/.mimir/episodes.jsonl`,
feeding the same auto-consolidate pipeline as Claude Code, Cline, and
Hermes.
