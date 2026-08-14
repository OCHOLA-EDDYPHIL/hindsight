"""Strict canonical evidence contracts for controlled recommendation replays."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


CANONICALIZATION_ID = "hindsight.canonical-json.v1"
CAUSAL_ENVELOPE_SCHEMA_VERSION = 1
CAUSAL_EVIDENCE_SCHEMA_VERSION = 1
MAX_SAFE_JSON_INTEGER = (2**53) - 1
GOVERNED_MEMORY_PROMPT_MARKER = "<declared-governed-memory-intervention>"


class CausalEvidenceError(ValueError):
    """Raised when evidence cannot be represented without ambiguity."""


def strict_json_loads(value: str) -> Any:
    """Decode JSON while rejecting duplicate keys and non-standard numbers."""

    def reject_constant(constant: str) -> None:
        raise CausalEvidenceError(f"non-finite JSON number is forbidden: {constant}")

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise CausalEvidenceError(f"duplicate JSON object key is forbidden: {key}")
            result[key] = item
        return result

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CausalEvidenceError("evidence JSON is invalid") from exc
    return _validate_canonical_value(decoded, path="$")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one accepted UTF-8 representation of an evidence value."""

    normalized = _validate_canonical_value(value, path="$")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return a labeled digest of strict canonical JSON."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def text_sha256(value: str) -> str:
    """Return the exact UTF-8 digest of one rendered prompt or template."""

    _validate_canonical_value(value, path="$")
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def json_contract_value(value: Any) -> Any:
    """Convert explicitly supported database scalars before canonicalization."""

    if value is None or type(value) in {str, bool, int, float}:
        return _validate_canonical_value(value, path="$")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CausalEvidenceError("naive evidence timestamps are forbidden")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [json_contract_value(item) for item in value]
    if isinstance(value, tuple):
        return [json_contract_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: json_contract_value(item) for key, item in value.items()}
    raise CausalEvidenceError(
        f"unsupported evidence value type: {type(value).__module__}.{type(value).__qualname__}"
    )


def build_causal_envelope(
    *,
    identity: Mapping[str, Any],
    invariant_inputs: Mapping[str, Any],
    permitted_intervention: Mapping[str, Any],
    actual_decision_inputs: Mapping[str, Any],
    rendered_prompt_sha256: list[str],
    decision_output: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a self-validating causal envelope without reordering input arrays."""

    payload = {
        "schema_version": CAUSAL_ENVELOPE_SCHEMA_VERSION,
        "canonicalization": CANONICALIZATION_ID,
        "identity": json_contract_value(identity),
        "invariant_inputs": json_contract_value(invariant_inputs),
        "invariant_inputs_sha256": canonical_sha256(json_contract_value(invariant_inputs)),
        "permitted_intervention": json_contract_value(permitted_intervention),
        "permitted_intervention_sha256": canonical_sha256(
            json_contract_value(permitted_intervention)
        ),
        "actual_decision_inputs": json_contract_value(actual_decision_inputs),
        "actual_decision_inputs_sha256": canonical_sha256(
            json_contract_value(actual_decision_inputs)
        ),
        "rendered_prompt_sha256": json_contract_value(rendered_prompt_sha256),
        "decision_output": json_contract_value(decision_output),
    }
    payload["envelope_sha256"] = canonical_sha256(payload)
    return payload


def validated_causal_envelope(value: Any) -> dict[str, Any] | None:
    """Return a complete untampered envelope or ``None`` for legacy/incomplete data."""

    if not isinstance(value, dict):
        return None
    required = {
        "schema_version",
        "canonicalization",
        "identity",
        "invariant_inputs",
        "invariant_inputs_sha256",
        "permitted_intervention",
        "permitted_intervention_sha256",
        "actual_decision_inputs",
        "actual_decision_inputs_sha256",
        "rendered_prompt_sha256",
        "decision_output",
        "envelope_sha256",
    }
    if set(value) != required:
        return None
    if (
        value.get("schema_version") != CAUSAL_ENVELOPE_SCHEMA_VERSION
        or value.get("canonicalization") != CANONICALIZATION_ID
        or not isinstance(value.get("identity"), dict)
        or not isinstance(value.get("invariant_inputs"), dict)
        or not isinstance(value.get("permitted_intervention"), dict)
        or not isinstance(value.get("actual_decision_inputs"), dict)
        or not isinstance(value.get("rendered_prompt_sha256"), list)
        or not isinstance(value.get("decision_output"), dict)
    ):
        return None
    try:
        if value["invariant_inputs_sha256"] != canonical_sha256(value["invariant_inputs"]):
            return None
        if value["permitted_intervention_sha256"] != canonical_sha256(
            value["permitted_intervention"]
        ):
            return None
        if value["actual_decision_inputs_sha256"] != canonical_sha256(
            value["actual_decision_inputs"]
        ):
            return None
        requests = value["actual_decision_inputs"].get("ordered_model_requests")
        if not isinstance(requests, list) or any(
            not isinstance(request, dict) or not isinstance(request.get("prompt"), str)
            for request in requests
        ):
            return None
        if value["rendered_prompt_sha256"] != [
            text_sha256(request["prompt"]) for request in requests
        ]:
            return None
        unsigned = {key: item for key, item in value.items() if key != "envelope_sha256"}
        if value["envelope_sha256"] != canonical_sha256(unsigned):
            return None
    except CausalEvidenceError:
        return None
    return value


def _validate_canonical_value(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        if not -MAX_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER:
            raise CausalEvidenceError(f"integer outside the interoperable range at {path}")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise CausalEvidenceError(f"non-finite number at {path}")
        if value == 0.0:
            return 0
        if value.is_integer() and -MAX_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER:
            return int(value)
        return value
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise CausalEvidenceError(f"non-NFC string at {path}")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CausalEvidenceError(f"invalid Unicode string at {path}") from exc
        return value
    if type(value) is list:
        return [
            _validate_canonical_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise CausalEvidenceError(f"non-string object key at {path}")
            _validate_canonical_value(key, path=f"{path}.<key>")
            normalized[key] = _validate_canonical_value(item, path=f"{path}.{key}")
        return normalized
    raise CausalEvidenceError(
        f"unsupported canonical value at {path}: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )
