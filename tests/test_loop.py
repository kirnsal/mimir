"""C5 integration — the end-to-end "Mimir runs on Mimir" loop. Tests first (TDD RED).

Proves the centerpiece deterministically (no live LLM, no tokens): the only
variable across arms is Mimir.
- COLD (memory-off) and COLD+naive (ungated context-stuffing) both fail; WARM
  (confidence-gated recall) succeeds — so the *gate* is what wins, not raw recall.
- the loop actually runs C1 capture -> C2 consolidate -> C4 recall (not pre-seeded).
- attribution headline: ablating the one fix-lesson drops the WARM score (its credit).
- safety: the poisoned/quarantined lesson is excluded from WARM recall.
"""
from bench import loop
from bench.harness import COLD, COLD_NAIVE, WARM, ablation_credit
from mimir.store import InMemoryLessonStore
from mimir.mcp_server import recall


def test_warm_beats_cold_and_naive_on_handbuilt_tasks():
    store = InMemoryLessonStore()
    loop.seed_poison(store)  # a known-bad lesson lives in the store (poisoning demo)

    cold, naive, warm = loop.run_loop(store, loop.TASKS, key="k", seed=0)

    assert cold.arm == COLD and naive.arm == COLD_NAIVE and warm.arm == WARM
    assert cold.success_rate == 0.0          # no guidance
    assert naive.success_rate == 0.0         # stuffed with the trap -> misled
    assert warm.success_rate == 1.0          # gated recall -> only the good lesson
    assert warm.success_rate > naive.success_rate > cold.success_rate - 0.0001


def test_loop_admits_the_fix_lesson_via_consolidation():
    store = InMemoryLessonStore()
    loop.run_loop(store, loop.TASKS, key="k", seed=0)
    # the consolidation step (C2) wrote the fix lessons into the store
    learned = {lo.rule for lo in store.active()}
    for task in loop.TASKS:
        assert task.fix_rule in learned


def test_ablation_credits_the_fix_lesson():
    store = InMemoryLessonStore()
    loop.run_loop(store, loop.TASKS, key="k", seed=0)
    task = loop.TASKS[0]
    lessons = recall(store, task.prompt).lessons
    fix = next(lo for lo in lessons if lo.rule == task.fix_rule)

    credit = ablation_credit(task, lessons, fix.id, loop.scripted_solver, seed=0)
    assert credit == 1.0  # removing it flips the task from solved to failed


def test_poisoned_lesson_excluded_from_warm_recall():
    store = InMemoryLessonStore()
    loop.seed_poison(store)
    loop.run_loop(store, loop.TASKS, key="k", seed=0)
    task = loop.TASKS[0]
    recalled = {lo.rule for lo in recall(store, task.prompt).lessons}
    assert task.trap_rule not in recalled
