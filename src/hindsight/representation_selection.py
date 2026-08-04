"""Select one candidate-neutral retrieval representation on development evidence."""

from __future__ import annotations

from typing import Any

from hindsight.embeddings import GEMINI_REPRESENTATIONS
from hindsight.evidence_archive import canonical_json_bytes, sha256_hex

MINIMUM_MARGIN = 0.02
TIE_TOLERANCE = 0.005
MAX_DISTANCE = 0.35


def select_representation(report: dict[str, Any]) -> dict[str, Any]:
    """Apply the frozen parity, coverage, margin, and tie rules."""

    if report.get("schema_version") != 2:
        raise ValueError("unsupported representation matrix")
    development_sha256 = str(report.get("development_sha256") or "")
    if len(development_sha256) != 64:
        raise ValueError("representation matrix is not bound to the development split")
    templates = report.get("representations")
    if not isinstance(templates, dict) or set(templates) != set(GEMINI_REPRESENTATIONS):
        raise ValueError("representation matrix is incomplete")
    profiles = report.get("embedding_profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(GEMINI_REPRESENTATIONS):
        raise ValueError("representation matrix profiles are incomplete")
    for name, profile in profiles.items():
        _validate_profile(profile, representation=name)
    baseline = _validated_items(templates["raw_control"])
    baseline_within = {
        item_id: {
            row["candidate_token"]
            for row in item["direct"]
            if float(row["distance"]) <= MAX_DISTANCE
        }
        for item_id, item in baseline.items()
    }
    scores = {}
    reasons = {}
    for name in GEMINI_REPRESENTATIONS:
        items = _validated_items(templates[name])
        if set(items) != set(baseline):
            raise ValueError("representations evaluated different development items")
        failures = []
        margins = []
        for item_id, item in items.items():
            direct = item["direct"]
            indexed = item["indexed"]
            if not _rankings_match(direct, indexed):
                failures.append(f"{item_id}:index_parity")
                continue
            target = str(item["target_token"])
            if not direct or direct[0]["candidate_token"] != target:
                failures.append(f"{item_id}:target_rank")
                continue
            target_distance = float(direct[0]["distance"])
            if target_distance > MAX_DISTANCE:
                failures.append(f"{item_id}:target_cutoff")
                continue
            current_within = {
                row["candidate_token"] for row in direct if float(row["distance"]) <= MAX_DISTANCE
            }
            if not baseline_within[item_id].issubset(current_within):
                failures.append(f"{item_id}:coverage_regression")
                continue
            competitors = [
                float(row["distance"]) for row in direct if row["candidate_token"] != target
            ]
            if not competitors:
                failures.append(f"{item_id}:missing_competitor")
                continue
            margins.append(min(competitors) - target_distance)
        score = min(margins, default=float("-inf"))
        if score < MINIMUM_MARGIN:
            failures.append("minimum_margin")
        if failures:
            reasons[name] = failures
        else:
            scores[name] = score
    if not scores:
        raise RuntimeError("no retrieval representation satisfies the frozen gates")
    ordered = sorted(scores, key=lambda name: scores[name], reverse=True)
    winner = ordered[0]
    tied = [name for name in ordered[1:] if scores[winner] - scores[name] < TIE_TOLERANCE]
    if tied:
        raise RuntimeError("representation matrix ended in a predeclared tie")
    if winner == "raw_control":
        raise RuntimeError("raw control won; protected evaluation must not proceed")
    return {
        "schema_version": 2,
        "development_sha256": development_sha256,
        "representation_matrix_sha256": sha256_hex(canonical_json_bytes(report)),
        "selected_representation": winner,
        "embedding_profile": profiles[winner],
        "minimum_margin": scores[winner],
        "eligible_scores": scores,
        "ineligible_reasons": reasons,
        "max_distance": MAX_DISTANCE,
        "reranking": False,
        "fallback": False,
    }


def build_representation_matrix(
    *,
    development_package: dict[str, Any],
    evaluations: dict[str, list[dict[str, Any]]],
    embedding_profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build one sanitized matrix bound only to the released development split."""

    if development_package.get("schema_version") != 4:
        raise ValueError("representation selection requires a v4 development package")
    if development_package.get("split") != "development":
        raise ValueError("representation selection may use only the development split")
    variants = development_package.get("variants")
    if not isinstance(variants, list) or len(variants) != 12:
        raise ValueError("representation selection requires twelve development variants")
    if set(evaluations) != set(GEMINI_REPRESENTATIONS):
        raise ValueError("representation evaluations are incomplete")
    if set(embedding_profiles) != set(GEMINI_REPRESENTATIONS):
        raise ValueError("representation profiles are incomplete")
    expected_items = {str(row.get("variant_id") or "") for row in variants}
    if "" in expected_items or len(expected_items) != len(variants):
        raise ValueError("development variant identities must be nonempty and unique")
    for name, rows in evaluations.items():
        observed = {str(row.get("item_token") or "") for row in rows}
        if observed != expected_items:
            raise ValueError(f"{name} evaluated a different development split")
        _validate_profile(embedding_profiles[name], representation=name)
    return {
        "schema_version": 2,
        "development_sha256": sha256_hex(canonical_json_bytes(development_package)),
        "representations": evaluations,
        "embedding_profiles": embedding_profiles,
    }


def _validated_items(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("representation requires development item results")
    result = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "item_token",
            "target_token",
            "direct",
            "indexed",
        }:
            raise ValueError("representation result exposes metadata or has an invalid schema")
        token = str(item["item_token"])
        if not token or token in result:
            raise ValueError("development item tokens must be unique")
        for field in ("direct", "indexed"):
            rows = item[field]
            if not isinstance(rows, list) or not rows:
                raise ValueError("representation rankings must be nonempty")
            if any(set(row) != {"candidate_token", "distance"} for row in rows):
                raise ValueError("representation rankings expose forbidden metadata")
        result[token] = item
    return result


def _rankings_match(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    if len(left) != len(right):
        return False
    return all(
        str(a["candidate_token"]) == str(b["candidate_token"])
        and abs(float(a["distance"]) - float(b["distance"])) <= 1e-6
        for a, b in zip(left, right, strict=True)
    )


def _validate_profile(profile: Any, *, representation: str) -> None:
    required = {
        "profile_id",
        "provider",
        "model",
        "dimensions",
        "capability",
        "encoder_revision",
        "configuration",
        "max_distance",
        "representation",
    }
    if not isinstance(profile, dict) or set(profile) != required:
        raise ValueError("representation matrix contains an invalid embedding profile")
    if (
        profile["representation"] != representation
        or profile["provider"] != "gemini"
        or profile["dimensions"] != 1024
        or profile["capability"] != "semantic"
        or float(profile["max_distance"]) != MAX_DISTANCE
        or not str(profile["profile_id"])
    ):
        raise ValueError("representation matrix profile differs from the frozen contract")
