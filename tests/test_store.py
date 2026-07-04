"""C3 — bi-temporal LESSON store. Tests written first (TDD RED).

Behaviour under test (PRD §6/§7 FR2+FR7, BUILD_SPEC C3):
- supersede preserves the prior version (bi-temporal: never hard-delete)
- rollback reactivates a superseded lesson
- active() excludes superseded/invalidated lessons
- snapshot() is deterministic (reproducibility, feeds C5)
"""
from datetime import datetime, timezone

from mimir.models import Lesson
from mimir.store import InMemoryLessonStore

FIXED_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _lesson(rule, confidence=0.7, **kw):
    return Lesson(rule=rule, confidence=confidence, **kw)


def test_add_and_get_roundtrip():
    store = InMemoryLessonStore()
    lid = store.add(_lesson("check permission boundaries"))
    got = store.get(lid)
    assert got is not None
    assert got.rule == "check permission boundaries"
    assert got.status == "active"
    assert got.invalid_at is None


def test_supersede_preserves_prior_version():
    store = InMemoryLessonStore()
    old_id = store.add(_lesson("retry network calls once", confidence=0.5))
    new_id = store.supersede(old_id, _lesson("retry network calls with backoff", confidence=0.8))

    old = store.get(old_id)
    new = store.get(new_id)
    # prior version survives, marked invalid (bi-temporal)
    assert old is not None
    assert old.status == "superseded"
    assert old.invalid_at is not None
    # new version is active and records what it contradicts
    assert new.status == "active"
    assert old_id in new.contradicts
    # active set excludes the superseded one
    assert {lo.id for lo in store.active()} == {new_id}


def test_rollback_reactivates_superseded_lesson():
    store = InMemoryLessonStore()
    old_id = store.add(_lesson("a"))
    store.supersede(old_id, _lesson("b"))

    store.rollback(old_id)

    rolled = store.get(old_id)
    assert rolled.status == "active"
    assert rolled.invalid_at is None
    assert old_id in {lo.id for lo in store.active()}


def test_snapshot_is_deterministic_and_order_independent():
    store_a = InMemoryLessonStore()
    store_a.add(_lesson("x", id="L1", valid_from=FIXED_TS))
    store_a.add(_lesson("y", id="L2", valid_from=FIXED_TS))
    snap = store_a.snapshot()
    assert snap == store_a.snapshot()  # stable across calls

    store_b = InMemoryLessonStore()
    store_b.add(_lesson("y", id="L2", valid_from=FIXED_TS))
    store_b.add(_lesson("x", id="L1", valid_from=FIXED_TS))
    assert store_b.snapshot() == snap  # independent of insertion order
