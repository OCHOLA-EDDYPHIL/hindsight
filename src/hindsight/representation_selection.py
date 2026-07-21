"""Select one candidate-neutral retrieval representation on development evidence."""

from __future__ import annotations

from typing import Any

from hindsight.embeddings import GEMINI_REPRESENTATIONS

MINIMUM_MARGIN = 0.02
TIE_TOLERANCE = 0.005
MAX_DISTANCE = 0.35


def select_representation(report: dict[str, Any]) -> dict[str, Any]:
    """Apply the frozen parity, coverage, margin, and tie rules."""

    if report.get("schema_version") != 1:
        raise ValueError("unsupported representation matrix")
    templates = report.get("representations")
    if not isinstance(templates, dict) or set(templates) != set(GEMINI_REPRESENTATIONS):
        raise ValueError("representation matrix is incomplete")
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
            if direct != indexed:
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
        "schema_version": 1,
        "selected_representation": winner,
        "minimum_margin": scores[winner],
        "eligible_scores": scores,
        "ineligible_reasons": reasons,
        "max_distance": MAX_DISTANCE,
        "reranking": False,
        "fallback": False,
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
