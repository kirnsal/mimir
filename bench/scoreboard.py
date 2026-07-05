"""Scoreboard orchestrator — run the COLD/NAIVE/WARM arms live and emit data.js.

Reuses bench.live.run_live per repeat (not run_live_repeated, which discards each
Report.records and so can't feed the per-task grid). Aggregates per-arm success
rate bands and per-task pass counts, then writes a `window.RESULTS = {...}` file
that bench/scoreboard/index.html renders. The live model is the default solver;
inject `solver=` for zero-token tests.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import mean
from typing import Callable, Optional

from bench.harness import PASS_SCORE
from bench.live import TASKS, run_live, seed_poison
from mimir.store import InMemoryLessonStore

# Harness arm label -> data.js key. run_live returns (cold, naive, warm) in order,
# so we map positionally; these labels are what the HTML shows.
ARM_KEYS = ("cold", "naive", "warm")
ARM_LABELS = {
    "cold": "COLD (no memory)",
    "naive": "NAIVE (ungated)",
    "warm": "WARM (Mimir, gated)",
}

DEFAULT_OUT = "bench/scoreboard/data.js"


def _passed(report, task_id: str) -> bool:
    rec = next(r for r in report.records if r["task_id"] == task_id)
    return rec["score"] >= PASS_SCORE


def build_results(tasks, *, repeats: int = 3, seed: int = 0, model: str = "default",
                  solver: Optional[Callable] = None) -> dict:
    """Run the three arms `repeats` times on a fresh poisoned store each pass and
    aggregate into the data.js payload."""
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    rates = {k: [] for k in ARM_KEYS}
    passes = {t.id: {k: 0 for k in ARM_KEYS} for t in tasks}

    for _ in range(repeats):
        store = InMemoryLessonStore()
        seed_poison(store, tasks)              # rebuilt each pass: consolidate mutates it
        reports = dict(zip(ARM_KEYS, run_live(store, tasks, key="scoreboard",
                                              solver=solver, seed=seed)))
        for key, rep in reports.items():
            rates[key].append(rep.success_rate)
            for t in tasks:
                if _passed(rep, t.id):
                    passes[t.id][key] += 1

    arms = [{"key": k, "label": ARM_LABELS[k],
             "success_rate": mean(rates[k]), "min": min(rates[k]), "max": max(rates[k])}
            for k in ARM_KEYS]
    task_rows = [{"id": t.id, **{k: passes[t.id][k] for k in ARM_KEYS}} for t in tasks]
    warm_m, cold_m, naive_m = mean(rates["warm"]), mean(rates["cold"]), mean(rates["naive"])
    return {
        "model": model, "repeats": repeats, "seed": seed,
        "arms": arms, "tasks": task_rows,
        "lift": {"cold_to_warm": warm_m - cold_m, "naive_to_warm": warm_m - naive_m},
    }


def write_data_js(results: dict, out_path) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.RESULTS = " + json.dumps(results, indent=2) + ";\n",
                   encoding="utf-8")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run the Mimir scoreboard benchmark.")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    model = os.environ.get("MIMIR_CLAUDE_MODEL") or "default"
    results = build_results(TASKS, repeats=args.repeats, seed=args.seed, model=model)
    write_data_js(results, args.out)
    print(f"wrote {args.out}  (model={model}, repeats={args.repeats}, seed={args.seed})")


if __name__ == "__main__":  # pragma: no cover
    main()
