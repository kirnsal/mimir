"""C3 — bi-temporal LESSON store.

A thin, swappable interface. This in-memory reference implementation lets the
rest of the system (C2 consolidation, C4 retrieval, C5 benchmark) be built and
tested without standing up Cognee. The Cognee-backed adapter implements the same
surface later (PRD §8: storage behind a thin `mimir` interface, backend swappable).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from mimir.models import ACTIVE, RETIRED, SUPERSEDED, Lesson


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryLessonStore:
    """Bi-temporal lesson store: supersede-not-delete, rollback-able, snapshot-able."""

    def __init__(self) -> None:
        self._lessons: dict[str, Lesson] = {}

    def add(self, lesson: Lesson) -> str:
        if not lesson.id:
            lesson.id = uuid.uuid4().hex
        if lesson.valid_from is None:
            lesson.valid_from = _now()
        self._lessons[lesson.id] = lesson
        return lesson.id

    def get(self, lesson_id: str) -> Optional[Lesson]:
        return self._lessons.get(lesson_id)

    def supersede(self, old_id: str, new_lesson: Lesson) -> str:
        """Close the old version (bi-temporal) and admit the winner (FR2)."""
        old = self._require(old_id)
        old.status = SUPERSEDED
        old.invalid_at = _now()  # never hard-delete; the prior version stays auditable (FR7)
        if old_id not in new_lesson.contradicts:
            new_lesson.contradicts.append(old_id)
        return self.add(new_lesson)

    def rollback(self, lesson_id: str) -> None:
        """Reactivate a superseded lesson (FR7 governance/rollback)."""
        lesson = self._require(lesson_id)
        lesson.status = ACTIVE
        lesson.invalid_at = None

    def retire(self, lesson_id: str) -> None:
        """Explicit `forget`: retire a lesson for good (bi-temporal — never hard-delete)."""
        lesson = self._require(lesson_id)
        lesson.status = RETIRED
        lesson.invalid_at = _now()

    def protect(self, lesson_id: str) -> None:
        """Pin a lesson: exempt from auto-supersede and circuit-breaker quarantine.
        Human-set only (no CLI/MCP surface yet -- store API is the workflow for now)."""
        self._require(lesson_id).protected = True

    def unprotect(self, lesson_id: str) -> None:
        self._require(lesson_id).protected = False

    def active(self) -> list[Lesson]:
        return [lo for lo in self._lessons.values()
                if lo.status == ACTIVE and lo.invalid_at is None]

    def all(self) -> list[Lesson]:
        """Every lesson, ungated — incl. quarantined/superseded (the naive arm + audit)."""
        return list(self._lessons.values())

    def snapshot(self) -> str:
        """Deterministic, order-independent JSON export (reproducibility for C5)."""
        rows = sorted((self._serialize(lo) for lo in self._lessons.values()),
                      key=lambda r: r["id"])
        return json.dumps(rows, sort_keys=True)

    def _require(self, lesson_id: str) -> Lesson:
        lesson = self._lessons.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson id: {lesson_id}")
        return lesson

    @staticmethod
    def _serialize(lesson: Lesson) -> dict:
        d = asdict(lesson)
        for key in ("valid_from", "invalid_at", "last_validated"):
            value = d[key]
            d[key] = value.isoformat() if isinstance(value, datetime) else None
        return d
