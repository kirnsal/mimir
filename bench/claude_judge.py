"""Live C2 judge + held-out probe — real Claude doing FR1 extraction and FR3 admission.

Swaps the deterministic stand-ins from bench/live.py for real model calls. Like
claude_cli.py, every call goes through run_claude's `_runner` seam, so the wiring
is verified with ZERO tokens; only `python -m bench.live` spends anything.

The judge is a single batched structured call (memory constraint: ~10x cheaper
than per-criterion calls). The probe runs the real solver on a held-out task set.
"""
from __future__ import annotations

import json
import re
from statistics import mean
from typing import Callable, Optional

from bench.claude_cli import run_claude
from mimir.consolidate import Verdict
from mimir.models import Episode

_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_FAIL = Verdict(rule="", specificity=0.0, generalizability=0.0, non_sycophancy=0.0)


def _clamp(x) -> float:
    return max(0.0, min(1.0, float(x)))


def _parse_verdict(text: str) -> Verdict:
    """Tolerant JSON -> Verdict. Fail-closed: anything unparseable scores 0 so the gate rejects it."""
    m = _OBJ.search(text)
    if not m:
        return _FAIL
    try:
        d = json.loads(m.group(0))
        rule = str(d["rule"]).strip()
        if not rule:
            return _FAIL
        return Verdict(
            rule=rule,
            specificity=_clamp(d["specificity"]),
            generalizability=_clamp(d["generalizability"]),
            non_sycophancy=_clamp(d["non_sycophancy"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _FAIL


_JUDGE_PROMPT = (
    "An AI coding agent failed a task. Read the episode and distill ONE durable, "
    "reusable lesson that would prevent this failure next time — a specific directive, "
    "not 'be careful'. Then score the lesson 0..1 on three axes:\n"
    "- specificity: actionable, names the concrete fix\n"
    "- generalizability: applies beyond this one instance\n"
    "- non_sycophancy: a genuine engineering insight, not user-flattery\n\n"
    "Episode:\n  action: {action}\n  context: {context}\n  consequence: {consequence}\n\n"
    "Reply with ONLY a JSON object, no prose:\n"
    '{{"rule": "...", "specificity": 0.0, "generalizability": 0.0, "non_sycophancy": 0.0}}'
)


def make_live_judge(*, runner: Optional[Callable] = None):
    """FR1 live judge: one batched structured call per failed EPISODE -> scored Verdict."""

    def judge(ep: Episode) -> Verdict:
        prompt = _JUDGE_PROMPT.format(
            action=ep.action, context=ep.context, consequence=ep.consequence)
        return _parse_verdict(run_claude(prompt, _runner=runner))

    return judge


def make_solver_probe(held_out, solver):
    """FR3 held-out probe: real solver pass-rate on tasks the lesson was NOT extracted from.

    ponytail: faithful but pricey — epsilon_admit calls this twice per candidate lesson,
    so cost = 2 * len(held_out) solver calls per lesson. Only runs in a funded live run;
    shrink held_out or cache the baseline score if the bill bites.
    """

    def probe(lessons: list) -> float:
        if not held_out:
            return 0.0
        return mean(t.verify(solver(t.payload, lessons)) for t in held_out)

    return probe
