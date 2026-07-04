"""C3 backend — semantic LESSON recall over a vector index, Cognee-backed.

The spec (BUILD_SPEC C3) puts the store behind a thin swappable interface and
names Cognee (Kuzu graph + LanceDB vector) as the backend. This module delivers
the vector half: a `CogneeLessonStore` that keeps the proven bi-temporal CRUD
(inherited from InMemoryLessonStore, so every C3 contract test still holds) and
adds `semantic_recall` over a pluggable `VectorIndex`.

Three indexes implement the same seam:
  - `LanceDBVectorIndex`  — LanceDB (Cognee's own vector engine) via its sync API;
    a real on-disk vector DB that runs live on this box.
  - `CogneeVectorIndex`   — cognee's full LanceDBAdapter (async graph+vector DB);
    code-complete but its async writer hangs on Py3.14/Windows (see note below).
  - `InProcessVectorIndex` — a pure-Python cosine index, zero deps, never blocks.

The embedding function is injected (the same DI pattern as the live judge/probe/
CLI runner): a hash embedder for tests (no key, no download, deterministic), a
real model for a live demo. So semantic recall runs and is testable token-free,
and Cognee slots in wherever its native async cooperates.

ponytail: default index is in-process (zero deps, never blocks). For a live vector
DB on this box, pass `LanceDBVectorIndex(url=...)` — LanceDB's sync writer works
fine here; only cognee's async adapter (`CogneeVectorIndex`) hangs on Py3.14/Windows.
All three share one seam, so the backend is a constructor arg, not a rewrite.
"""
from __future__ import annotations

import hashlib
import math
import uuid
from typing import Callable, Optional, Protocol

from mimir.models import Lesson
from mimir.store import InMemoryLessonStore

# Deterministic namespace so lesson-id -> vector-store UUID is stable across runs
# (cognee DataPoint.id must be a UUID; our lesson ids are arbitrary strings).
_NS = uuid.UUID("6d696d69-7200-0000-0000-000000000001")


def lesson_uuid(lesson_id: str) -> uuid.UUID:
    return uuid.uuid5(_NS, lesson_id)


# --- embedding (injectable) --------------------------------------------------

Embed = Callable[[list[str]], list[list[float]]]
_DIM = 64


def hash_embed(texts: list[str], *, dim: int = _DIM) -> list[list[float]]:
    """A token-hashing bag-of-words embedder: no model, no network, deterministic.

    Good enough for tests and a zero-dependency demo — shared vocabulary between a
    query and a lesson lands them near each other in cosine space. Swap for a real
    sentence embedder (or cognee's engine) via the `embed` arg for production recall.
    """
    vecs: list[list[float]] = []
    for text in texts:
        v = [0.0] * dim
        for tok in text.lower().split():
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
            v[h % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        vecs.append([x / norm for x in v])
    return vecs


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # inputs are unit vectors


# --- vector index seam -------------------------------------------------------

class VectorIndex(Protocol):
    def upsert(self, lesson_id: str, text: str) -> None: ...
    def query(self, text: str, k: int) -> list[tuple[str, float]]: ...


class InProcessVectorIndex:
    """Pure-Python cosine index. Zero deps, never blocks — the safe default."""

    def __init__(self, embed: Embed = hash_embed) -> None:
        self._embed = embed
        self._vecs: dict[str, list[float]] = {}

    def upsert(self, lesson_id: str, text: str) -> None:
        self._vecs[lesson_id] = self._embed([text])[0]

    def query(self, text: str, k: int) -> list[tuple[str, float]]:
        q = self._embed([text])[0]
        scored = [(lid, _cosine(q, v)) for lid, v in self._vecs.items()]
        scored.sort(key=lambda p: p[1], reverse=True)
        return [(lid, s) for lid, s in scored[:k] if s > 0.0]


class LanceDBVectorIndex:
    """LanceDB (Cognee's own vector engine) via its SYNC API — runs live here.

    Cognee stores vectors in LanceDB. Its async adapter hangs on this Py3.14 /
    Windows box, but LanceDB's sync writer works fine, so this talks to the same
    real on-disk vector database directly. Same seam as the in-process index;
    persists to `url`. Embedding is injected (unit vectors -> cosine metric).
    """

    TABLE = "mimir_lessons"

    def __init__(self, url: str, *, embed: Embed = hash_embed, dim: int = _DIM) -> None:
        import lancedb

        self._embed = embed
        self._dim = dim
        self._db = lancedb.connect(url)
        self._tbl = None  # created lazily on first upsert (needs a row for schema)

    def _row(self, lesson_id: str, text: str) -> dict:
        return {"lesson_id": lesson_id, "vector": self._embed([text])[0]}

    def upsert(self, lesson_id: str, text: str) -> None:
        row = self._row(lesson_id, text)
        if self._tbl is None:
            try:  # reuse a table left by a prior process (consolidate -> serve); else create
                self._tbl = self._db.open_table(self.TABLE)
            except (FileNotFoundError, ValueError):
                self._tbl = self._db.create_table(self.TABLE, [row])
                return
        # ponytail: lesson ids are our own slugs (no quotes), so this filter is safe;
        # switch to a parameterised delete if ids ever come from untrusted input.
        self._tbl.delete(f"lesson_id = '{lesson_id}'")
        self._tbl.add([row])

    def query(self, text: str, k: int) -> list[tuple[str, float]]:
        if self._tbl is None:
            return []
        q = self._embed([text])[0]
        rows = self._tbl.search(q).metric("cosine").limit(k).to_list()
        # cosine distance = 1 - similarity; keep only positive-similarity hits
        out = [(r["lesson_id"], 1.0 - float(r["_distance"])) for r in rows]
        return [(lid, s) for lid, s in out if s > 0.0]


class CogneeVectorIndex:
    """cognee's real LanceDB vector store behind the same seam.

    Lazily imports cognee so the core package stays dependency-free. Runs every
    call through a private event loop (cognee's adapter is async-first). Use where
    LanceDB's native async writer works; on platforms where it hangs, use
    InProcessVectorIndex — both implement VectorIndex identically.
    """

    COLLECTION = "mimir_lessons"

    def __init__(self, url: str, *, embed: Embed = hash_embed, dim: int = _DIM) -> None:
        import asyncio

        from cognee.infrastructure.databases.vector.lancedb.LanceDBAdapter import (
            LanceDBAdapter,
        )
        from cognee.infrastructure.engine import DataPoint

        class _Engine:  # duck-typed cognee EmbeddingEngine (only these are called)
            async def embed_text(self, data: list[str]) -> list[list[float]]:
                return embed(data)

            def get_vector_size(self) -> int:
                return dim

            async def get_tokenizer(self):  # pragma: no cover - cognee optional hook
                return None

        class _LessonPoint(DataPoint):
            lesson_id: str
            text: str
            metadata: dict = {"index_fields": ["text"]}

        self._loop = asyncio.new_event_loop()
        self._adapter = LanceDBAdapter(url=url, api_key=None, embedding_engine=_Engine())
        self._Point = _LessonPoint

    def upsert(self, lesson_id: str, text: str) -> None:
        point = self._Point(id=lesson_uuid(lesson_id), lesson_id=lesson_id, text=text)
        self._loop.run_until_complete(
            self._adapter.create_data_points(self.COLLECTION, [point])
        )

    def query(self, text: str, k: int) -> list[tuple[str, float]]:
        results = self._loop.run_until_complete(
            self._adapter.search(self.COLLECTION, query_text=text, limit=k)
        )
        out: list[tuple[str, float]] = []
        for r in results:
            payload = getattr(r, "payload", None) or {}
            lid = payload.get("lesson_id")
            if lid is not None:
                out.append((lid, float(getattr(r, "score", 0.0))))
        return out


# --- the store ---------------------------------------------------------------

class CogneeLessonStore(InMemoryLessonStore):
    """Bi-temporal lesson store + semantic recall over a vector index.

    CRUD/bi-temporal behaviour is inherited unchanged (C3 contract holds). Every
    active-making write mirrors the lesson's `rule` into the vector index so
    `semantic_recall` ranks lessons by meaning, not lexical overlap. The index is
    injected; it defaults to the no-dep in-process cosine index.
    """

    def __init__(self, index: Optional[VectorIndex] = None) -> None:
        super().__init__()
        self._index: VectorIndex = index or InProcessVectorIndex()

    def add(self, lesson: Lesson) -> str:
        lid = super().add(lesson)
        self._index.upsert(lid, lesson.rule)
        return lid

    def semantic_recall(self, query: str, *, k: int = 5) -> list[Lesson]:
        """Return ACTIVE lessons ranked by vector similarity to `query`.

        The gate stays authoritative: index hits are intersected with active(), so
        superseded/quarantined lessons never surface even if still in the index.
        """
        active = {lo.id: lo for lo in self.active()}
        ranked = self._index.query(query, k=k * 2)  # over-fetch; active() prunes
        out = [active[lid] for lid, _ in ranked if lid in active]
        return out[:k]
