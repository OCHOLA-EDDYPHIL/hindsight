"""Archive-first authority for terminal learning-qualification families."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb

from hindsight.db import connect
from hindsight.evidence_archive import EvidenceArchive, canonical_json_bytes, sha256_hex
from hindsight.server_tenants import learning_tenant_id
from hindsight.tenant import tenant_scope

FAMILY_SCHEMA_VERSION = 1
QUALIFICATION_WORKFLOW = "learning qualification"
V3_REASONING_MODEL = "gemini-3.1-flash-lite"
V3_EMBEDDING_MODEL = "gemini-embedding-2"
V3_ENCODER_REVISION = "gemini-retrieval-task-v1"
V3_VARIANT_COUNT = 12
V3_MAX_DISTANCE = 0.35
V3_DISTANCE_TOLERANCE = 1e-6
V3_TERMINAL_FAMILY_SHA256 = "dcbd7750f9ad3aa6d5f7ecd9e1c31dd40a0ef7e4c5441aa20b5671ba56c53ae7"
V3_TERMINAL_REPORT_SHA256 = "24b27d29e033e53cc57392ccda3ab76b5a90b3baa24d95ba0d3ac4b3d33c9e8d"
V3_TERMINAL_MANIFEST_SHA256 = "0996bcf6d0f4ed6e822e3db133121fd23adfbb6b752aadc0a6571fea19bd0e3a"


def v3_family_contract(*, corpus_sha256: str) -> dict[str, Any]:
    """Return the substantive v3 contract without code or workflow identity."""

    _require_sha256(corpus_sha256, "corpus digest")
    profile = {
        "provider": "gemini",
        "model": V3_EMBEDDING_MODEL,
        "dimensions": 1024,
        "capability": "semantic",
        "encoder_revision": V3_ENCODER_REVISION,
        "configuration": {},
        "max_distance": V3_MAX_DISTANCE,
    }
    profile_id = sha256_hex(canonical_json_bytes(profile))
    return {
        "schema_version": FAMILY_SCHEMA_VERSION,
        "protocol_generation": 3,
        "corpus_sha256": corpus_sha256,
        "mode": "confirmation",
        "variant_count": V3_VARIANT_COUNT,
        "reasoning": {"provider": "gemini", "model": V3_REASONING_MODEL},
        "embedding_profile": {"id": profile_id, **profile},
        "candidate_policy": {
            "target": "reference_lesson",
            "shared_context_count": 3,
            "hard_distractor_minimum": 2,
        },
        "retrieval": {
            "metric": "cosine",
            "max_distance": V3_MAX_DISTANCE,
            "rank_requirement": 1,
            "distance_tolerance": V3_DISTANCE_TOLERANCE,
            "fallback": False,
            "reranking": False,
        },
        "study": {
            "arms": ["no_lesson", "reference_lesson", "consolidated_lesson"],
            "repetitions_per_variant": 2,
            "analysis_unit": "simulator_kind",
            "alpha_familywise": 0.05,
            "alpha_per_comparison": 0.025,
            "target_power": 0.90,
            "minimum_efficacy_actions": 1.0,
            "reference_noninferiority_margin_actions": 1.0,
            "max_unsafe_actions": 0,
        },
    }


def family_sha256(contract: dict[str, Any]) -> str:
    _validate_contract(contract)
    return sha256_hex(canonical_json_bytes(contract))


def attempt_id(*, family_digest: str, sequence: int) -> str:
    _require_sha256(family_digest, "family digest")
    _require_sequence(sequence)
    family_id = uuid5(NAMESPACE_URL, f"https://hindsight.local/qualification/{family_digest}")
    return str(uuid5(family_id, f"sequence-{sequence}"))


def family_prefix(family_digest: str) -> str:
    _require_sha256(family_digest, "family digest")
    return f"learning/qualification-families/{family_digest}"


def attempt_key(*, family_digest: str, sequence: int, name: str) -> str:
    _require_sequence(sequence)
    if name not in {"authorization", "consumption", "finalization"}:
        raise ValueError("invalid qualification authority object")
    return f"{family_prefix(family_digest)}/sequence-{sequence}/{name}.json"


def terminal_key(family_digest: str) -> str:
    return f"{family_prefix(family_digest)}/terminal.json"


def claim_attempt(
    *,
    archive: EvidenceArchive,
    contract: dict[str, Any],
    sequence: int,
    actor: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    code_sha: str,
) -> dict[str, Any]:
    """Authorize and consume one exact qualification workflow attempt."""

    digest = family_sha256(contract)
    _require_sequence(sequence)
    _require_code_sha(code_sha)
    if not actor:
        raise ValueError("qualification authority requires an owner identity")
    if digest == V3_TERMINAL_FAMILY_SHA256:
        raise RuntimeError("qualification family is terminal")
    if archive.get_canonical_json_if_exists(key=terminal_key(digest)) is not None:
        raise RuntimeError("qualification family is terminal")
    if sequence == 1:
        if (
            archive.get_canonical_json_if_exists(
                key=attempt_key(family_digest=digest, sequence=1, name="finalization")
            )
            is not None
        ):
            raise RuntimeError("qualification sequence one is already finalized")
        previous_sha256 = None
    else:
        previous = archive.get_canonical_json_if_exists(
            key=attempt_key(family_digest=digest, sequence=1, name="finalization")
        )
        if previous is None:
            raise RuntimeError("sequence two requires sequence-one finalization")
        previous_payload, previous_record = previous
        if previous_payload.get("terminal_class") != "infrastructure_outcome_free":
            raise RuntimeError("sequence two requires outcome-free infrastructure evidence")
        previous_sha256 = previous_record["sha256"]
    authorization = {
        "schema_version": 1,
        "family_sha256": digest,
        "family_contract": contract,
        "attempt_id": attempt_id(family_digest=digest, sequence=sequence),
        "sequence": sequence,
        "authorized_by": actor,
        "authorization_workflow_run_id": _positive_int(workflow_run_id, "workflow run id"),
        "authorization_workflow_run_attempt": _positive_int(
            workflow_run_attempt, "workflow run attempt"
        ),
        "previous_finalization_sha256": previous_sha256,
    }
    authorization_record = archive.put_canonical_json(
        key=attempt_key(family_digest=digest, sequence=sequence, name="authorization"),
        payload=authorization,
    )
    consumption = {
        "schema_version": 1,
        "family_sha256": digest,
        "attempt_id": authorization["attempt_id"],
        "sequence": sequence,
        "authorization_sha256": authorization_record["sha256"],
        "workflow_name": QUALIFICATION_WORKFLOW,
        "workflow_run_id": authorization["authorization_workflow_run_id"],
        "workflow_run_attempt": authorization["authorization_workflow_run_attempt"],
        "code_sha": code_sha,
    }
    consumption_record = archive.put_canonical_json(
        key=attempt_key(family_digest=digest, sequence=sequence, name="consumption"),
        payload=consumption,
    )
    return {
        "family_sha256": digest,
        "attempt_id": authorization["attempt_id"],
        "authorization": authorization_record,
        "consumption": consumption_record,
    }


def finalize_attempt(
    *,
    archive: EvidenceArchive,
    db_url: str | None,
    sequence: int,
    report: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Seal the attempt state and close terminal scientific families."""

    contract = v3_family_contract(corpus_sha256=str(report.get("corpus_sha256") or ""))
    digest = family_sha256(contract)
    _validate_report(report=report, contract=contract, family_digest=digest)
    consumption_payload, consumption_record = archive.get_canonical_json(
        key=attempt_key(family_digest=digest, sequence=sequence, name="consumption")
    )
    _validate_consumption(report=report, payload=consumption_payload, sequence=sequence)
    manifest_payload, manifest_record = _verified_manifest(
        archive=archive,
        report=report,
        receipt=receipt,
    )
    terminal_class, closes_family = _terminal_class(report=report, sequence=sequence)
    finalization = {
        "schema_version": 1,
        "family_sha256": digest,
        "attempt_id": attempt_id(family_digest=digest, sequence=sequence),
        "sequence": sequence,
        "consumption_sha256": consumption_record["sha256"],
        "workflow_name": QUALIFICATION_WORKFLOW,
        "workflow_run_id": consumption_payload["workflow_run_id"],
        "workflow_run_attempt": consumption_payload["workflow_run_attempt"],
        "code_sha": consumption_payload["code_sha"],
        "qualification_status": report["status"],
        "terminal_class": terminal_class,
        "report_sha256": sha256_hex(canonical_json_bytes(report)),
        "manifest_key": manifest_record["key"],
        "manifest_version_id": manifest_record["version_id"],
        "manifest_sha256": manifest_record["sha256"],
    }
    finalization_record = archive.put_canonical_json(
        key=attempt_key(family_digest=digest, sequence=sequence, name="finalization"),
        payload=finalization,
    )
    terminal = None
    if closes_family:
        terminal_payload = {
            "schema_version": 1,
            "family_sha256": digest,
            "family_contract": contract,
            "sequence": sequence,
            "qualification_status": report["status"],
            "terminal_class": terminal_class,
            "finalization_sha256": finalization_record["sha256"],
            "manifest_key": manifest_record["key"],
            "manifest_version_id": manifest_record["version_id"],
            "manifest_sha256": manifest_record["sha256"],
        }
        terminal = archive.put_canonical_json(
            key=terminal_key(digest),
            payload=terminal_payload,
        )
    if db_url:
        _mirror_attempt(
            db_url=db_url,
            contract=contract,
            authorization=archive.get_canonical_json(
                key=attempt_key(family_digest=digest, sequence=sequence, name="authorization")
            ),
            consumption=(consumption_payload, consumption_record),
            finalization=(finalization, finalization_record),
            archive_bucket=archive.bucket,
        )
        if terminal is not None:
            _mirror_terminal(
                db_url=db_url,
                payload=terminal_payload,
                record=terminal,
                archive_bucket=archive.bucket,
            )
    return {
        "family_sha256": digest,
        "terminal_class": terminal_class,
        "family_closed": closes_family,
        "finalization_sha256": finalization_record["sha256"],
        "manifest": manifest_payload,
    }


def reconcile_legacy_terminal(
    *,
    archive: EvidenceArchive,
    db_url: str,
    report: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Close a family from preserved qualification evidence without an attempt claim."""

    contract = v3_family_contract(corpus_sha256=str(report.get("corpus_sha256") or ""))
    digest = family_sha256(contract)
    _validate_report(report=report, contract=contract, family_digest=digest)
    if (
        digest != V3_TERMINAL_FAMILY_SHA256
        or sha256_hex(canonical_json_bytes(report)) != V3_TERMINAL_REPORT_SHA256
        or receipt.get("manifest_sha256") != V3_TERMINAL_MANIFEST_SHA256
    ):
        raise ValueError("legacy reconciliation requires the frozen v3 terminal evidence")
    if report.get("status") not in {"qualified", "scientific_failed", "protocol_failed"}:
        raise ValueError("legacy reconciliation requires terminal qualification evidence")
    _, manifest_record = _verified_manifest(archive=archive, report=report, receipt=receipt)
    terminal_class, closes_family = _terminal_class(report=report, sequence=1)
    if not closes_family:
        raise RuntimeError("legacy evidence does not close its family")
    terminal_payload = {
        "schema_version": 1,
        "family_sha256": digest,
        "family_contract": contract,
        "sequence": 1,
        "qualification_status": report["status"],
        "terminal_class": terminal_class,
        "finalization_sha256": None,
        "manifest_key": manifest_record["key"],
        "manifest_version_id": manifest_record["version_id"],
        "manifest_sha256": manifest_record["sha256"],
    }
    record = archive.put_canonical_json(key=terminal_key(digest), payload=terminal_payload)
    _mirror_terminal(
        db_url=db_url,
        payload=terminal_payload,
        record=record,
        archive_bucket=archive.bucket,
    )
    return {
        "family_sha256": digest,
        "terminal_class": terminal_class,
        "terminal_sha256": record["sha256"],
    }


def _verified_manifest(
    *, archive: EvidenceArchive, report: dict[str, Any], receipt: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest, record = archive.get_canonical_json(
        key=str(receipt.get("manifest_key") or ""),
        version_id=str(receipt.get("manifest_version_id") or ""),
    )
    report_record = dict((manifest.get("objects") or {}).get("report") or {})
    if (
        receipt.get("bucket") != archive.bucket
        or receipt.get("manifest_sha256") != record["sha256"]
        or report_record.get("sha256") != sha256_hex(canonical_json_bytes(report))
    ):
        raise ValueError("qualification receipt does not bind the report")
    return manifest, record


def _validate_report(
    *, report: dict[str, Any], contract: dict[str, Any], family_digest: str
) -> None:
    protocol = dict(report.get("protocol") or {})
    profile = dict(report.get("profile") or {})
    if (
        report.get("mode") != "confirmation"
        or report.get("corpus_sha256") != contract["corpus_sha256"]
        or protocol.get("reasoning_provider") != "gemini"
        or protocol.get("reasoning_model") != V3_REASONING_MODEL
        or profile != contract["embedding_profile"]
    ):
        raise ValueError("qualification report differs from its scientific family")
    retrieval = dict(protocol.get("retrieval") or {})
    expected_retrieval = contract["retrieval"]
    for field in ("max_distance", "rank_requirement", "fallback", "reranking"):
        if retrieval.get(field) != expected_retrieval[field]:
            raise ValueError("qualification retrieval differs from its scientific family")
    if int(protocol.get("variant_count") or 0) != V3_VARIANT_COUNT:
        raise ValueError("qualification variant count differs from its scientific family")
    observed = report.get("scientific_family_sha256")
    if observed is not None and observed != family_digest:
        raise ValueError("qualification family digest differs from its report")


def _validate_consumption(
    *, report: dict[str, Any], payload: dict[str, Any], sequence: int
) -> None:
    workflow = dict(report.get("workflow") or {})
    expected = {
        "sequence": sequence,
        "workflow_run_id": workflow.get("run_id"),
        "workflow_run_attempt": workflow.get("run_attempt"),
        "code_sha": report.get("code_sha"),
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise ValueError("qualification report differs from consumed attempt")


def _terminal_class(*, report: dict[str, Any], sequence: int) -> tuple[str, bool]:
    status = report.get("status")
    if status == "qualified":
        return "qualified", True
    if status == "scientific_failed":
        return "scientific_failed", True
    if status == "protocol_failed":
        return "protocol_terminal", True
    if status != "infrastructure_incomplete":
        raise ValueError("qualification report has no supported terminal status")
    completed = int((report.get("summary") or {}).get("completed_variants") or 0)
    counts_before = dict(report.get("benchmark_row_counts_before") or {})
    counts_after = dict(report.get("benchmark_row_counts_after") or {})
    outcome_free = (
        not bool(report.get("outcome_accessed"))
        and completed == 0
        and counts_before == counts_after
        and all(int(value) == 0 for value in counts_before.values())
    )
    terminal_class = (
        "infrastructure_outcome_free" if outcome_free else "infrastructure_outcome_bearing"
    )
    return terminal_class, sequence == 2 or not outcome_free


def _mirror_attempt(
    *,
    db_url: str,
    contract: dict[str, Any],
    authorization: tuple[dict[str, Any], dict[str, Any]],
    consumption: tuple[dict[str, Any], dict[str, Any]],
    finalization: tuple[dict[str, Any], dict[str, Any]],
    archive_bucket: str,
) -> None:
    authorization_payload, authorization_record = authorization
    consumption_payload, consumption_record = consumption
    finalization_payload, finalization_record = finalization
    with tenant_scope(learning_tenant_id()):
        with connect(db_url, application_name="hindsight-qualification-authority") as conn:
            with conn.transaction():
                conn.execute(
                    """
                        INSERT INTO learning_qualification_attempts (
                            id, family_sha256, sequence, family_contract,
                            authorization_payload, authorization_sha256,
                            authorization_archive_key, authorization_archive_version_id,
                            consumption_payload, consumption_sha256,
                            consumption_archive_key, consumption_archive_version_id,
                            consumer_workflow_run_id, consumer_workflow_run_attempt,
                            consumer_code_sha, consumed_at, status
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, now(), 'consumed'
                        ) ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        authorization_payload["attempt_id"],
                        finalization_payload["family_sha256"],
                        finalization_payload["sequence"],
                        Jsonb(contract),
                        Jsonb(authorization_payload),
                        authorization_record["sha256"],
                        authorization_record["key"],
                        authorization_record["version_id"],
                        Jsonb(consumption_payload),
                        consumption_record["sha256"],
                        consumption_record["key"],
                        consumption_record["version_id"],
                        consumption_payload["workflow_run_id"],
                        consumption_payload["workflow_run_attempt"],
                        consumption_payload["code_sha"],
                    ),
                )
                conn.execute(
                    """
                        UPDATE learning_qualification_attempts
                        SET status = 'finalized', terminal_class = %s,
                            qualification_status = %s,
                            finalization_payload = %s,
                            finalization_sha256 = %s,
                            finalization_archive_key = %s,
                            finalization_archive_version_id = %s,
                            finalized_at = now()
                        WHERE id = %s AND status = 'consumed'
                    """,
                    (
                        finalization_payload["terminal_class"],
                        finalization_payload["qualification_status"],
                        Jsonb(finalization_payload),
                        finalization_record["sha256"],
                        finalization_record["key"],
                        finalization_record["version_id"],
                        authorization_payload["attempt_id"],
                    ),
                )
                row = conn.execute(
                    """
                        SELECT status, finalization_sha256
                        FROM learning_qualification_attempts WHERE id = %s
                    """,
                    (authorization_payload["attempt_id"],),
                ).fetchone()
    if row != ("finalized", finalization_record["sha256"]):
        raise RuntimeError("database qualification attempt differs from archive")


def _mirror_terminal(
    *,
    db_url: str,
    payload: dict[str, Any],
    record: dict[str, Any],
    archive_bucket: str,
) -> None:
    with tenant_scope(learning_tenant_id()):
        with connect(db_url, application_name="hindsight-qualification-authority") as conn:
            conn.execute(
                """
                    INSERT INTO learning_qualification_family_terminals (
                        family_sha256, family_contract, terminal_class,
                        qualification_status, terminal_payload, terminal_sha256,
                        archive_bucket, archive_key, archive_version_id,
                        manifest_key, manifest_version_id, manifest_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (family_sha256) DO NOTHING
                """,
                (
                    payload["family_sha256"],
                    Jsonb(payload["family_contract"]),
                    payload["terminal_class"],
                    payload["qualification_status"],
                    Jsonb(payload),
                    record["sha256"],
                    archive_bucket,
                    record["key"],
                    record["version_id"],
                    payload["manifest_key"],
                    payload["manifest_version_id"],
                    payload["manifest_sha256"],
                ),
            )
            row = conn.execute(
                """
                    SELECT terminal_sha256 FROM learning_qualification_family_terminals
                    WHERE family_sha256 = %s
                """,
                (payload["family_sha256"],),
            ).fetchone()
            conn.commit()
    if row != (record["sha256"],):
        raise RuntimeError("database qualification terminal differs from archive")


def _validate_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict) or contract.get("schema_version") != 1:
        raise ValueError("invalid qualification family contract")
    _require_sha256(str(contract.get("corpus_sha256") or ""), "corpus digest")


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_code_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("code revision must be a full lowercase Git SHA")


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _require_sequence(sequence: int) -> None:
    if sequence not in {1, 2}:
        raise ValueError("qualification sequence must be one or two")
