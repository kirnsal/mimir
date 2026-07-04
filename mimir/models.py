"""Typed memory nodes (PRD §6 canonical schema).

Only the nodes C3 needs are defined here. KNOWLEDGE and the ephemeral
scratchpad layer arrive with later components.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# LESSON lifecycle states (PRD §6 / FR4 / FR2).
ACTIVE = "active"
QUARANTINED = "quarantined"
RETIRED = "retired"
SUPERSEDED = "superseded"


@dataclass
class Episode:
    """Raw experience — the X/Y/Z triple (PRD §6). A MISTAKE = failed outcome_score."""

    action: str = ""          # X — what the agent did
    context: str = ""         # Y — situation / preconditions
    consequence: str = ""     # Z — what happened
    outcome_score: Optional[float] = None  # from the deterministic verifier; None until scored
    session_id: str = ""
    task_id: str = ""
    timestamp: Optional[datetime] = None
    id: str = ""


@dataclass
class Lesson:
    """Wisdom — distilled, evidence-gated, bi-temporal (PRD §6 / FR2 / FR7)."""

    rule: str                                  # specific directive, not "be careful"
    confidence: float = 0.0                    # 0..1, moves only on scored evidence (FR3)
    status: str = ACTIVE                        # active | quarantined | retired | superseded
    supporting_episodes: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)
    provenance: str = ""                        # who/what derived it (FR7 governance)
    citation: str = ""                          # HMAC-SHA-256 signature from C2 (FR7 integrity)
    repo_hash: str = ""                          # env the lesson was learned in (BUILD_SPEC C3)
    dependency_fingerprint: str = ""             # ^ recorded-only in v1; relevance/decay hook for FR6 (H2)
    id: str = ""
    valid_from: Optional[datetime] = None       # bi-temporal: when this version became active
    invalid_at: Optional[datetime] = None       # set on supersede; never hard-delete
    last_validated: Optional[datetime] = None
