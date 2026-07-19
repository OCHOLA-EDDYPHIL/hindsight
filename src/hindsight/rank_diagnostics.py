"""Outcome-free helpers for comparing direct and indexed vector ordering."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any


def opaque_token(*parts: str) -> str:
    """Return a stable opaque identifier without exposing diagnostic text."""

    material = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Compute cosine distance with explicit dimension and zero-vector checks."""

    if len(left) != len(right):
        raise ValueError("vectors must have equal dimensions")
    if not left:
        raise ValueError("vectors must not be empty")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("vectors must have nonzero magnitude")
    return 1.0 - (dot / (left_norm * right_norm))


def ranked_candidates(
    *,
    query_embedding: Sequence[float],
    candidates: Sequence[dict[str, Any]],
    target_token: str,
    max_distance: float,
) -> dict[str, Any]:
    """Rank opaque candidates and return only sanitized diagnostic metadata."""

    if not 0 < max_distance <= 2:
        raise ValueError("max_distance must be within (0, 2]")
    if not candidates:
        raise ValueError("at least one candidate is required")
    if sum(str(row["token"]) == target_token for row in candidates) != 1:
        raise ValueError("exactly one target candidate is required")

    measured = []
    target_distance = None
    for candidate in candidates:
        distance = cosine_distance(query_embedding, candidate["embedding"])
        token = str(candidate["token"])
        if token == target_token:
            target_distance = distance
        if distance <= max_distance:
            measured.append(
                {
                    "candidate_token": token,
                    "candidate_role": str(candidate["role"]),
                    "distance": distance,
                }
            )
    measured.sort(key=lambda row: (row["distance"], row["candidate_token"]))
    for rank, row in enumerate(measured, start=1):
        row["rank"] = rank

    target_rank = next(
        (row["rank"] for row in measured if row["candidate_token"] == target_token),
        None,
    )
    competing = [
        row["distance"] for row in measured if row["candidate_token"] != target_token
    ]
    margin = None
    if target_distance is not None and competing:
        margin = min(competing) - target_distance
    return {
        "target_rank": target_rank,
        "target_within_cutoff": target_rank is not None,
        "target_rank_one": target_rank == 1,
        "target_margin": margin,
        "rankings": measured,
    }


def indexed_candidates(
    *,
    hits: Sequence[dict[str, Any]],
    identity_by_memory_id: dict[str, tuple[str, str]],
    target_token: str,
) -> dict[str, Any]:
    """Sanitize CockroachDB search hits into the direct-ranking report shape."""

    rankings = []
    for rank, hit in enumerate(hits, start=1):
        memory_id = str(hit["id"])
        identity = identity_by_memory_id.get(memory_id)
        if identity is None:
            raise ValueError("indexed result contains an unknown diagnostic memory")
        token, role = identity
        rankings.append(
            {
                "candidate_token": token,
                "candidate_role": role,
                "distance": float(hit["distance"]),
                "rank": rank,
            }
        )
    target = next((row for row in rankings if row["candidate_token"] == target_token), None)
    competing = [
        row["distance"] for row in rankings if row["candidate_token"] != target_token
    ]
    margin = None
    if target is not None and competing:
        margin = min(competing) - target["distance"]
    return {
        "target_rank": target["rank"] if target is not None else None,
        "target_within_cutoff": target is not None,
        "target_rank_one": bool(target and target["rank"] == 1),
        "target_margin": margin,
        "rankings": rankings,
    }
