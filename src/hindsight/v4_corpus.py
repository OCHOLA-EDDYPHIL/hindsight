"""Construct and split a blinded simulator-grounded learning corpus."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
import base64
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from hindsight.benchmark import IncidentSimulator
from hindsight.evidence_archive import EvidenceArchive, canonical_json_bytes, sha256_hex
from hindsight.gemini import GeminiCredentialPool
from hindsight.reasoning import (
    DEFAULT_GEMINI_MODEL,
    GeminiReasoningProvider,
    ReasoningProvider,
    ReasoningProviderError,
    ReasoningRequest,
    retrying_reasoning_provider,
)

SCHEMA_VERSION = 4
REFERENCE_SOURCE = "sealed-v4-simulator-contract-v1"
SIMULATOR_KINDS = (
    "retry_amplification",
    "cache_stampede",
    "connection_leak",
    "hot_partition",
    "poison_message",
    "lock_contention",
)
ACTION_BINDINGS = {
    "retry_amplification": {
        "diagnostic": "inspect_dependency",
        "recovery": "throttle_retries",
        "unsafe": "scale_workers",
        "ineffective": "inspect_queue",
    },
    "cache_stampede": {
        "diagnostic": "inspect_cache",
        "recovery": "coalesce_requests",
        "unsafe": "scale_origin",
        "ineffective": "inspect_origin",
    },
    "connection_leak": {
        "diagnostic": "inspect_transactions",
        "recovery": "isolate_leak",
        "unsafe": "increase_pool",
        "ineffective": "inspect_pool",
    },
    "hot_partition": {
        "diagnostic": "inspect_key_distribution",
        "recovery": "salt_hot_key",
        "unsafe": "add_consumers",
        "ineffective": "inspect_partition_load",
    },
    "poison_message": {
        "diagnostic": "inspect_failed_payload",
        "recovery": "quarantine_message",
        "unsafe": "add_consumers",
        "ineffective": "inspect_consumer_lag",
    },
    "lock_contention": {
        "diagnostic": "inspect_lock_graph",
        "recovery": "terminate_blocker",
        "unsafe": "increase_timeouts",
        "ineffective": "inspect_query_latency",
    },
}
CONSTRUCTION_PROTOCOL_REVISION = "v4-gemini-construction-v2"
DRAFTER_ROLE = "drafter"
ADJUDICATOR_ROLES = ("adjudicator_primary", "adjudicator_secondary")
DRAFTER_MODEL = DEFAULT_GEMINI_MODEL
ADJUDICATOR_MODELS = (DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)
SLOTS_PER_FAMILY = 15
ACCEPTED_PER_FAMILY = 10
MAX_TARGET_QUERY_OVERLAP = 0.35
MAX_DISTRACTOR_QUERY_OVERLAP = 0.25
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HEX = re.compile(r"[0-9a-f]+")


class SlotRejected(ValueError):
    """A fixed construction slot did not satisfy the frozen protocol."""


class CorpusProviderUnavailable(RuntimeError):
    """The fixed corpus provider failed after the protected attempt began."""


@dataclass(frozen=True)
class GeminiJsonModel:
    """Pinned JSON-only Gemini caller for one construction role."""

    role_id: str
    model_id: str
    provider: ReasoningProvider
    max_tokens: int
    temperature: float

    @classmethod
    def create(
        cls,
        *,
        role_id: str,
        model_id: str,
        max_tokens: int,
        temperature: float,
        credential_pool: GeminiCredentialPool,
        max_attempts: int = 4,
    ) -> GeminiJsonModel:
        if role_id not in {DRAFTER_ROLE, *ADJUDICATOR_ROLES}:
            raise ValueError("unsupported Gemini corpus role")
        if model_id != DEFAULT_GEMINI_MODEL:
            raise ValueError("corpus construction requires the pinned Gemini model")
        provider = retrying_reasoning_provider(
            GeminiReasoningProvider(
                model_name=model_id,
                credential_pool=credential_pool,
            ),
            max_attempts=max_attempts,
        )
        return cls(
            role_id=role_id,
            model_id=model_id,
            provider=provider,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def generate(self, *, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            response = self.provider.generate(
                ReasoningRequest(
                    system=system,
                    prompt=prompt,
                    temperature=self.temperature,
                    max_output_tokens=self.max_tokens,
                    routing_key=(
                        f"{CONSTRUCTION_PROTOCOL_REVISION}:{self.role_id}:"
                        f"{sha256_hex(canonical_json_bytes(payload))}"
                    ),
                    response_json_schema=_response_schema(self.role_id),
                    thinking_budget=0,
                )
            )
        except ReasoningProviderError as exc:
            raise CorpusProviderUnavailable("Gemini corpus provider is unavailable") from exc
        if response.provider != "gemini" or response.model != self.model_id:
            raise CorpusProviderUnavailable("Gemini corpus provider identity drifted")
        try:
            result = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise SlotRejected("model returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise SlotRejected("model response must be one JSON object")
        return result


def construction_protocol() -> dict[str, Any]:
    """Return the complete immutable corpus-construction contract."""

    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_revision": CONSTRUCTION_PROTOCOL_REVISION,
        "families": list(SIMULATOR_KINDS),
        "slots_per_family": SLOTS_PER_FAMILY,
        "accepted_per_family": ACCEPTED_PER_FAMILY,
        "models": {
            "drafter": {
                "provider": "gemini",
                "role": DRAFTER_ROLE,
                "id": DRAFTER_MODEL,
                "temperature": 0.2,
                "max_tokens": 1800,
                "thinking_budget": 0,
                "response_schema_sha256": sha256_hex(
                    canonical_json_bytes(_response_schema(DRAFTER_ROLE))
                ),
            },
            "adjudicators": [
                {
                    "provider": "gemini",
                    "role": role,
                    "id": model,
                    "temperature": 0.0,
                    "max_tokens": 1200,
                    "thinking_budget": 0,
                    "response_schema_sha256": sha256_hex(
                        canonical_json_bytes(_response_schema(role))
                    ),
                }
                for role, model in zip(ADJUDICATOR_ROLES, ADJUDICATOR_MODELS, strict=True)
            ],
        },
        "prompt_revisions": {
            "draft": {
                "name": "simulator-grounded-draft-v1",
                "sha256": sha256_hex(_draft_system_prompt().encode()),
            },
            "adjudicator": {
                "name": "blinded-action-adjudication-v1",
                "sha256": sha256_hex(_adjudicator_system_prompt().encode()),
            },
            "owner_review": {
                "name": "single-owner-no-feedback-v1",
                "questions": [
                    "What is the clearest first response?",
                    "Are two or more answers reasonably defensible?",
                ],
            },
        },
        "action_bindings": ACTION_BINDINGS,
        "selection": "first-ten-eligible-in-fixed-slot-order",
        "lexical_overlap": {
            "target_query_maximum": MAX_TARGET_QUERY_OVERLAP,
            "distractor_query_maximum": MAX_DISTRACTOR_QUERY_OVERLAP,
        },
        "split": {
            "method": "post-seal-public-randomness-sha256-v1",
            "beacon_source": "nist-randomness-beacon-v2",
            "per_family": {
                "development": 2,
                "pilot": 2,
                "confirmation": 3,
                "retired": 3,
            },
        },
        "released_item_contract": {
            "reference_source": REFERENCE_SOURCE,
            "source_evidence": "simulator-grounded-draft-v1",
            "required_fields": [
                "source_summary",
                "root_cause",
                "resolution_action",
                "resolution_observation",
            ],
        },
    }


def protocol_sha256() -> str:
    return sha256_hex(canonical_json_bytes(construction_protocol()))


def construct_pool(
    *,
    pool_id: str,
    drafter: GeminiJsonModel,
    adjudicators: tuple[GeminiJsonModel, GeminiJsonModel],
    v3_corpus: dict[str, Any],
) -> dict[str, Any]:
    """Consume every fixed slot once and select the first eligible items."""

    if not pool_id or len(pool_id) > 128:
        raise ValueError("pool identity is required")
    if (drafter.role_id, drafter.model_id) != (DRAFTER_ROLE, DRAFTER_MODEL) or tuple(
        (item.role_id, item.model_id) for item in adjudicators
    ) != tuple(zip(ADJUDICATOR_ROLES, ADJUDICATOR_MODELS, strict=True)):
        raise ValueError("corpus construction requires the pinned model roles")
    v3_text = _v3_normalized_text(v3_corpus)
    accepted: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    status = "accepted"
    terminal_reason = None
    seen_text = set(v3_text)
    for simulator_kind in SIMULATOR_KINDS:
        family_accepted = 0
        for slot in range(1, SLOTS_PER_FAMILY + 1):
            slot_key = _slot_key(pool_id=pool_id, simulator_kind=simulator_kind, slot=slot)
            record: dict[str, Any] = {
                "simulator_kind": simulator_kind,
                "slot": slot,
                "slot_sha256": sha256_hex(slot_key.encode()),
            }
            try:
                draft = drafter.generate(
                    system=_draft_system_prompt(),
                    payload=_draft_payload(simulator_kind=simulator_kind),
                )
                record["draft_sha256"] = sha256_hex(canonical_json_bytes(draft))
                item = _validate_draft(
                    draft=draft,
                    simulator_kind=simulator_kind,
                    seen_text=seen_text,
                )
                replay = _simulator_replay(simulator_kind=simulator_kind)
                adjudications = []
                for adjudicator in adjudicators:
                    candidates = _adjudicator_candidates(
                        item=item,
                        secret=slot_key,
                        adjudicator_role=adjudicator.role_id,
                    )
                    result = adjudicator.generate(
                        system=_adjudicator_system_prompt(),
                        payload={
                            "incident": item["recurrence_query"],
                            "initial_observation": replay["initial_observation"],
                            "action_bindings": replay["action_bindings"],
                            "candidates": [
                                {"choice": row["choice"], "text": row["text"]} for row in candidates
                            ],
                        },
                    )
                    adjudications.append(
                        _validate_judgment(
                            judgment=result,
                            candidates=candidates,
                            role_id=adjudicator.role_id,
                            model_id=adjudicator.model_id,
                        )
                    )
                item["source_slot_sha256"] = record["slot_sha256"]
                item["simulator_replay"] = replay
                item["adjudications"] = adjudications
                item["definition_sha256"] = sha256_hex(canonical_json_bytes(item))
                record["eligible"] = True
                record["definition_sha256"] = item["definition_sha256"]
                if family_accepted < ACCEPTED_PER_FAMILY:
                    accepted.append(item)
                    family_accepted += 1
                    record["selected"] = True
                    seen_text.update(_item_normalized_text(item))
                else:
                    record["selected"] = False
            except SlotRejected as exc:
                record.update(eligible=False, selected=False, reason=str(exc))
            except CorpusProviderUnavailable:
                record.update(
                    eligible=False,
                    selected=False,
                    reason="gemini_provider_unavailable",
                )
                status = "infrastructure_failed"
                terminal_reason = "gemini_provider_unavailable"
            audit.append(record)
            if status != "accepted":
                break
        if status != "accepted":
            break
        if family_accepted != ACCEPTED_PER_FAMILY:
            status = "scientific_failed"
            terminal_reason = f"insufficient_eligible_{simulator_kind}"
            break
    if status == "accepted" and len(accepted) != len(SIMULATOR_KINDS) * ACCEPTED_PER_FAMILY:
        status = "scientific_failed"
        terminal_reason = "unbalanced_pool"
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "terminal_reason": terminal_reason,
        "pool_id": pool_id,
        "protocol": construction_protocol(),
        "protocol_sha256": protocol_sha256(),
        "items": accepted,
        "slot_audit": audit,
    }
    result["pool_sha256"] = sha256_hex(canonical_json_bytes(result))
    return result


def build_review_packet(
    *, pool: dict[str, Any], review_secret: str | None = None
) -> dict[str, Any]:
    """Create the frozen blinded order and choices for private owner review."""

    _validate_pool(pool)
    secret = review_secret or secrets.token_hex(32)
    if len(secret) < 32:
        raise ValueError("review secret is too short")
    ordered = sorted(
        pool["items"],
        key=lambda item: _keyed_digest(secret, "item", item["definition_sha256"]),
    )
    review_items = []
    for index, item in enumerate(ordered, start=1):
        choices = _review_choices(item=item, secret=secret)
        review_items.append(
            {
                "index": index,
                "scenario": item["recurrence_query"],
                "choices": [{"choice": row["choice"], "text": row["text"]} for row in choices],
                "target_choice": next(row["choice"] for row in choices if row["role"] == "target"),
                "definition_sha256": item["definition_sha256"],
            }
        )
    packet = {
        "schema_version": 1,
        "pool_sha256": pool["pool_sha256"],
        "review_secret": secret,
        "items": review_items,
    }
    packet["review_packet_sha256"] = sha256_hex(canonical_json_bytes(packet))
    return packet


def new_review_state(*, packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pool_sha256": packet["pool_sha256"],
        "review_packet_sha256": packet["review_packet_sha256"],
        "started_at": datetime.now(UTC).isoformat(),
        "decisions": [],
    }


def record_review_decision(
    *,
    packet: dict[str, Any],
    state: dict[str, Any],
    index: int,
    choice: str,
    ambiguous: bool,
) -> dict[str, Any]:
    """Append exactly one irreversible blinded owner decision."""

    _validate_review_binding(packet=packet, state=state)
    expected_index = len(state["decisions"]) + 1
    if index != expected_index or index > len(packet["items"]):
        raise ValueError("review decisions must be submitted once in frozen order")
    item = packet["items"][index - 1]
    if choice not in {row["choice"] for row in item["choices"]}:
        raise ValueError("review choice is not part of the current item")
    updated = json.loads(json.dumps(state))
    updated["decisions"].append(
        {
            "index": index,
            "choice": choice,
            "ambiguous": bool(ambiguous),
            "decided_at": datetime.now(UTC).isoformat(),
        }
    )
    if len(updated["decisions"]) == len(packet["items"]):
        updated["completed_at"] = datetime.now(UTC).isoformat()
    return updated


def finalize_review(
    *, pool: dict[str, Any], packet: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Accept only one complete, target-matching, unambiguous owner review."""

    _validate_pool(pool)
    _validate_review_binding(packet=packet, state=state)
    if len(state["decisions"]) != len(packet["items"]):
        raise RuntimeError("owner review is incomplete")
    accepted = all(
        decision["choice"] == packet["items"][index]["target_choice"] and not decision["ambiguous"]
        for index, decision in enumerate(state["decisions"])
    )
    if not accepted:
        raise RuntimeError("owner review did not accept the complete pool")
    review_record = json.loads(json.dumps(state))
    review_record["decision_sha256"] = sha256_hex(canonical_json_bytes(state["decisions"]))
    reviewed = {
        "schema_version": SCHEMA_VERSION,
        "pool": pool,
        "review_packet": packet,
        "owner_review": review_record,
    }
    reviewed["reviewed_pool_sha256"] = sha256_hex(canonical_json_bytes(reviewed))
    return reviewed


def split_reviewed_pool(
    *,
    reviewed_pool: dict[str, Any],
    sealed_manifest_sha256: str,
    sealed_at: datetime,
    beacon: dict[str, Any],
) -> dict[str, Any]:
    """Split a sealed reviewed pool using later public randomness."""

    if len(sealed_manifest_sha256) != 64 or not _HEX.fullmatch(sealed_manifest_sha256):
        raise ValueError("a sealed pool manifest digest is required")
    value = str(beacon.get("value") or "").lower()
    if len(value) not in {64, 128} or not _HEX.fullmatch(value):
        raise ValueError("public randomness must be a 256-bit or 512-bit hex value")
    try:
        published_at = datetime.fromisoformat(str(beacon["published_at"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("public randomness requires a publication timestamp") from exc
    if published_at.tzinfo is None:
        raise ValueError("public randomness timestamp must include a timezone")
    if published_at.astimezone(UTC) <= sealed_at.astimezone(UTC):
        raise ValueError("public randomness must be published after the pool was sealed")
    if beacon.get("source") != "nist-randomness-beacon-v2" or not str(
        beacon.get("pulse_uri") or ""
    ).startswith("https://beacon.nist.gov/beacon/2.0/pulse/time/"):
        raise ValueError("public randomness must identify one NIST beacon v2 pulse")
    pool = reviewed_pool.get("pool") or {}
    _validate_pool(pool)
    assignments: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("development", "pilot", "confirmation", "retired")
    }
    allocation = (
        *("development" for _ in range(2)),
        *("pilot" for _ in range(2)),
        *("confirmation" for _ in range(3)),
        *("retired" for _ in range(3)),
    )
    for simulator_kind in SIMULATOR_KINDS:
        family = [item for item in pool["items"] if item["simulator_kind"] == simulator_kind]
        family.sort(
            key=lambda item: sha256_hex(
                "\x1f".join(
                    (
                        "hindsight-v4-split-v1",
                        sealed_manifest_sha256,
                        value,
                        item["definition_sha256"],
                    )
                ).encode()
            )
        )
        for position, (split, item) in enumerate(zip(allocation, family, strict=True), start=1):
            assignments[split].append(
                _release_item(
                    item=item,
                    split=split,
                    public_id=sha256_hex(
                        f"{value}\x1f{simulator_kind}\x1f{position}\x1f{split}".encode()
                    ),
                )
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "reviewed_pool_sha256": reviewed_pool["reviewed_pool_sha256"],
        "sealed_manifest_sha256": sealed_manifest_sha256,
        "beacon": beacon,
        "development": assignments["development"],
        "pilot": assignments["pilot"],
        "confirmation": assignments["confirmation"],
        "retired_sha256": [
            sha256_hex(canonical_json_bytes(item)) for item in assignments["retired"]
        ],
    }


def build_study_manifest(
    *,
    code_sha: str,
    split_receipt: dict[str, Any],
    representation_selection: dict[str, Any],
) -> dict[str, Any]:
    """Bind the sealed corpus and selected development profile to one code revision."""

    if not re.fullmatch(r"[0-9a-f]{40,64}", code_sha):
        raise ValueError("study manifest requires one full code revision")
    required_split = {
        "reviewed_pool_sha256",
        "sealed_manifest_sha256",
        "beacon",
        "development_sha256",
        "protected",
        "retired_sha256",
    }
    if split_receipt.get("schema_version") != 1 or not required_split.issubset(split_receipt):
        raise ValueError("study manifest requires one complete split receipt")
    if representation_selection.get("schema_version") != 2:
        raise ValueError("study manifest requires one frozen representation selection")
    if representation_selection.get("development_sha256") != split_receipt["development_sha256"]:
        raise ValueError("representation selection differs from the development split")
    selected_profile = representation_selection.get("embedding_profile")
    selected_representation = representation_selection.get("selected_representation")
    matrix_sha256 = str(representation_selection.get("representation_matrix_sha256") or "")
    if (
        selected_representation not in {"generic_title", "applicability_instruction"}
        or len(matrix_sha256) != 64
        or not _HEX.fullmatch(matrix_sha256)
        or float(representation_selection.get("max_distance") or 0) != 0.35
        or representation_selection.get("reranking") is not False
        or representation_selection.get("fallback") is not False
    ):
        raise ValueError("representation selection differs from the frozen retrieval contract")
    if (
        not isinstance(selected_profile, dict)
        or not selected_profile.get("profile_id")
        or selected_profile.get("representation") != selected_representation
        or selected_profile.get("provider") != "gemini"
        or selected_profile.get("dimensions") != 1024
        or selected_profile.get("capability") != "semantic"
        or float(selected_profile.get("max_distance") or 0) != 0.35
    ):
        raise ValueError("representation selection has no embedding profile")
    protocol = construction_protocol()
    manifest = {
        "schema_version": 1,
        "code_sha": code_sha,
        "corpus": {
            "schema_version": SCHEMA_VERSION,
            "protocol_sha256": protocol_sha256(),
            "models": protocol["models"],
            "prompt_revisions": protocol["prompt_revisions"],
            "reviewed_pool_sha256": split_receipt["reviewed_pool_sha256"],
            "sealed_manifest_sha256": split_receipt["sealed_manifest_sha256"],
            "beacon": split_receipt["beacon"],
            "development_sha256": split_receipt["development_sha256"],
            "protected": split_receipt["protected"],
            "retired_sha256": split_receipt["retired_sha256"],
        },
        "retrieval": {
            "representation_matrix_sha256": matrix_sha256,
            "selected_representation": selected_representation,
            "embedding_profile": selected_profile,
            "max_distance": representation_selection["max_distance"],
            "rank_requirement": 1,
            "reranking": False,
            "fallback": False,
        },
    }
    manifest["manifest_sha256"] = sha256_hex(canonical_json_bytes(manifest))
    return manifest


def read_study_split(
    *, manifest: dict[str, Any], split: str, package: dict[str, Any]
) -> list[dict[str, Any]]:
    """Validate and return one manifest-bound released split for the study runner."""

    if split not in {"development", "pilot", "confirmation"}:
        raise ValueError("unsupported v4 study split")
    without_digest = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("manifest_sha256") != sha256_hex(canonical_json_bytes(without_digest)):
        raise ValueError("study manifest digest differs from its content")
    if package.get("schema_version") != SCHEMA_VERSION or package.get("split") != split:
        raise ValueError("released corpus package has an invalid split contract")
    variants = package.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ValueError("released corpus package has no variants")
    expected_count = {"development": 12, "pilot": 12, "confirmation": 18}[split]
    if len(variants) != expected_count:
        raise ValueError("released corpus package is not balanced")
    if split == "development":
        expected_sha256 = manifest["corpus"]["development_sha256"]
        if sha256_hex(canonical_json_bytes(package)) != expected_sha256:
            raise ValueError("development package differs from the study manifest")
    else:
        receipt = manifest["corpus"]["protected"].get(split) or {}
        if sha256_hex(canonical_json_bytes(package)) != receipt.get("sha256"):
            raise ValueError("protected package differs from the study manifest")
    for variant in variants:
        _validate_released_item(variant, split=split)
    return json.loads(json.dumps(variants))


def load_sealed_reviewed_pool(
    *, archive: EvidenceArchive, receipt: dict[str, Any]
) -> tuple[dict[str, Any], datetime]:
    """Load the exact reviewed pool and its immutable manifest time."""

    if receipt.get("bucket") != archive.bucket:
        raise ValueError("reviewed-pool receipt belongs to a different archive")
    manifest, record = archive.get_canonical_json(
        key=str(receipt.get("manifest_key") or ""),
        version_id=str(receipt.get("manifest_version_id") or ""),
    )
    if receipt.get("manifest_sha256") != record["sha256"]:
        raise ValueError("reviewed-pool receipt digest differs from the archive")
    reviewed_record = dict((manifest.get("objects") or {}).get("reviewed_pool") or {})
    reviewed, observed = archive.get_canonical_json(
        key=str(reviewed_record.get("key") or ""),
        version_id=str(reviewed_record.get("version_id") or ""),
    )
    if observed["sha256"] != reviewed_record.get("sha256"):
        raise ValueError("reviewed pool differs from its sealed manifest")
    head = archive.client.head_object(
        Bucket=archive.bucket,
        Key=record["key"],
        VersionId=record["version_id"],
    )
    sealed_at = head.get("LastModified")
    if not isinstance(sealed_at, datetime):
        raise RuntimeError("sealed pool manifest has no publication time")
    return reviewed, sealed_at


def put_protected_json(
    *, client: Any, bucket: str, key: str, kms_key_id: str, payload: Any
) -> dict[str, Any]:
    """Write one immutable KMS-encrypted protected corpus package."""

    if not bucket or not key.startswith("learning/protected-corpora/") or not kms_key_id:
        raise ValueError("protected corpus storage identity is invalid")
    body = canonical_json_bytes(payload)
    digest = sha256_hex(body)
    checksum = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=kms_key_id,
            BucketKeyEnabled=True,
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=checksum,
            Metadata={"sha256": digest},
            IfNoneMatch="*",
        )
        version_id = str(response.get("VersionId") or "")
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
        if code not in {"PreconditionFailed", "412"} and status != 412:
            raise
        head = client.head_object(Bucket=bucket, Key=key)
        version_id = str(head.get("VersionId") or "")
    if not version_id:
        raise RuntimeError("protected corpus archive returned no object version")
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    stream = response["Body"]
    try:
        observed = stream.read()
    finally:
        stream.close()
    if observed != body:
        raise RuntimeError("existing protected corpus package differs from expected content")
    retention = (
        client.get_object_retention(Bucket=bucket, Key=key, VersionId=version_id).get("Retention")
        or {}
    )
    if retention.get("Mode") != "GOVERNANCE" or not isinstance(
        retention.get("RetainUntilDate"), datetime
    ):
        raise RuntimeError("protected corpus package does not have archive retention")
    return {
        "bucket": bucket,
        "key": key,
        "version_id": version_id,
        "sha256": digest,
        "checksum_sha256": checksum,
        "kms_key_id": kms_key_id,
        "retain_until": retention["RetainUntilDate"].astimezone(UTC).isoformat(),
    }


def _response_schema(role_id: str) -> dict[str, Any]:
    if role_id == DRAFTER_ROLE:
        fields = (
            "source_summary",
            "root_cause",
            "resolution_action",
            "resolution_observation",
            "recurrence_query",
            "reference_lesson",
            "unsafe_distractor",
            "ineffective_distractor",
            "background_memory",
        )
        return {
            "type": "object",
            "properties": {field: {"type": "string", "minLength": 1} for field in fields},
            "required": list(fields),
            "additionalProperties": False,
        }
    if role_id in ADJUDICATOR_ROLES:
        return {
            "type": "object",
            "properties": {
                "selected_choice": {"type": "string", "minLength": 1},
                "ambiguous": {"type": "boolean"},
                "safe_order": {"type": "boolean"},
                "candidate_bindings": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string",
                        "enum": [
                            "safe_sequence",
                            "unsafe_action",
                            "ineffective_action",
                            "background",
                        ],
                    },
                },
            },
            "required": [
                "selected_choice",
                "ambiguous",
                "safe_order",
                "candidate_bindings",
            ],
            "additionalProperties": False,
        }
    raise ValueError("unsupported Gemini corpus role")


def _draft_system_prompt() -> str:
    return (
        "Draft one operational incident retrieval challenge as strict JSON. Use only the "
        "provided deterministic simulator behavior. Write a distinct source incident summary, "
        "root cause, bounded resolution action, and observed recovery suitable for normal "
        "incident consolidation. Separately write a recurrence scenario, one safe reference "
        "lesson that clearly requires diagnosis before recovery, two plausible hard distractors, "
        "and one unrelated background memory. Source evidence must state observed facts and must "
        "not copy the reference lesson. Do not include IDs, family names, action enum names, "
        "candidate roles, or commentary. Return exactly these string keys: source_summary, "
        "root_cause, resolution_action, resolution_observation, recurrence_query, "
        "reference_lesson, unsafe_distractor, ineffective_distractor, background_memory."
    )


def _draft_payload(*, simulator_kind: str) -> dict[str, Any]:
    bindings = ACTION_BINDINGS[simulator_kind]
    simulator = IncidentSimulator(simulator_kind)
    initial = simulator.observe(include_simulator_kind=False)
    diagnostic = simulator.step(bindings["diagnostic"])
    recovered = simulator.step(bindings["recovery"])
    return {
        "initial_observation": initial,
        "safe_diagnostic_result": diagnostic,
        "safe_recovery_result": recovered,
        "unsafe_action_result": _single_action_result(simulator_kind, bindings["unsafe"]),
        "ineffective_action_result": _single_action_result(simulator_kind, bindings["ineffective"]),
        "writing_constraints": {
            "no_action_names": list(bindings.values()),
            "single_unambiguous_first_response": True,
            "target_order": "diagnose_then_recover",
        },
    }


def _validate_draft(
    *, draft: dict[str, Any], simulator_kind: str, seen_text: set[str]
) -> dict[str, Any]:
    fields = {
        "source_summary",
        "root_cause",
        "resolution_action",
        "resolution_observation",
        "recurrence_query",
        "reference_lesson",
        "unsafe_distractor",
        "ineffective_distractor",
        "background_memory",
    }
    if set(draft) != fields:
        raise SlotRejected("draft schema is invalid")
    normalized = {key: _normalize_text(value) for key, value in draft.items()}
    if any(not value or len(value) > 2000 for value in normalized.values()):
        raise SlotRejected("draft text is empty or too long")
    if len(set(normalized.values())) != len(normalized):
        raise SlotRejected("draft contains duplicate text")
    if set(normalized.values()) & seen_text:
        raise SlotRejected("draft duplicates existing corpus text")
    query = normalized["recurrence_query"]
    if (
        max(
            _lexical_overlap(query, normalized["source_summary"]),
            _lexical_overlap(query, normalized["reference_lesson"]),
        )
        > MAX_TARGET_QUERY_OVERLAP
    ):
        raise SlotRejected("draft target repeats too much of the incident")
    if (
        max(
            _lexical_overlap(query, normalized["unsafe_distractor"]),
            _lexical_overlap(query, normalized["ineffective_distractor"]),
        )
        > MAX_DISTRACTOR_QUERY_OVERLAP
    ):
        raise SlotRejected("draft distractor repeats too much of the incident")
    source_evidence = "\n".join(
        normalized[field]
        for field in (
            "source_summary",
            "root_cause",
            "resolution_action",
            "resolution_observation",
        )
    )
    if normalized["reference_lesson"] in source_evidence:
        raise SlotRejected("draft source evidence copies the reference lesson")
    return {
        "simulator_kind": simulator_kind,
        "source_summary": normalized["source_summary"],
        "root_cause": normalized["root_cause"],
        "resolution_action": normalized["resolution_action"],
        "resolution_observation": normalized["resolution_observation"],
        "reference_source": REFERENCE_SOURCE,
        "recurrence_query": normalized["recurrence_query"],
        "reference_lesson": normalized["reference_lesson"],
        "context_memories": [
            {
                "role": "hard_distractor",
                "binding": "unsafe",
                "content": normalized["unsafe_distractor"],
            },
            {
                "role": "hard_distractor",
                "binding": "ineffective",
                "content": normalized["ineffective_distractor"],
            },
            {"role": "background", "binding": "none", "content": normalized["background_memory"]},
        ],
    }


def _simulator_replay(*, simulator_kind: str) -> dict[str, Any]:
    bindings = ACTION_BINDINGS[simulator_kind]
    target = IncidentSimulator(simulator_kind)
    initial = target.observe(include_simulator_kind=False)
    target.step(bindings["diagnostic"])
    target_result = target.step(bindings["recovery"])
    unsafe_result = _single_action_result(simulator_kind, bindings["unsafe"])
    ineffective_result = _single_action_result(simulator_kind, bindings["ineffective"])
    if not target.recovered or target.unsafe_actions or not target_result["recovered"]:
        raise SlotRejected("simulator target binding did not recover safely")
    if unsafe_result["recovered"] or not unsafe_result["unsafe"]:
        raise SlotRejected("simulator unsafe binding is not unsafe and ineffective")
    if ineffective_result["recovered"] or ineffective_result["unsafe"]:
        raise SlotRejected("simulator ineffective binding is not neutral and ineffective")
    return {
        "initial_observation": initial,
        "action_bindings": {
            "safe_sequence": [bindings["diagnostic"], bindings["recovery"]],
            "unsafe_action": [bindings["unsafe"]],
            "ineffective_action": [bindings["ineffective"]],
            "background": [],
        },
        "target_result": target_result,
        "unsafe_result": unsafe_result,
        "ineffective_result": ineffective_result,
    }


def _adjudicator_system_prompt() -> str:
    return (
        "Act as an independent incident-response adjudicator. Select the single clearest safe first "
        "response using only the incident, simulator observation, action bindings, and shuffled "
        "candidate text. Return strict JSON with selected_choice, ambiguous, safe_order, and "
        "candidate_bindings. candidate_bindings must map every choice to exactly one of "
        "safe_sequence, unsafe_action, ineffective_action, or background. Do not add commentary."
    )


def _validate_judgment(
    *,
    judgment: dict[str, Any],
    candidates: list[dict[str, str]],
    role_id: str,
    model_id: str,
) -> dict[str, Any]:
    required = {"selected_choice", "ambiguous", "safe_order", "candidate_bindings"}
    if set(judgment) != required or not isinstance(judgment["candidate_bindings"], dict):
        raise SlotRejected("adjudicator schema is invalid")
    by_choice = {row["choice"]: row for row in candidates}
    expected_bindings = {
        row["choice"]: {
            "target": "safe_sequence",
            "unsafe": "unsafe_action",
            "ineffective": "ineffective_action",
            "background": "background",
        }[row["role"]]
        for row in candidates
    }
    target = next(row["choice"] for row in candidates if row["role"] == "target")
    if (
        judgment["selected_choice"] != target
        or judgment["ambiguous"] is not False
        or judgment["safe_order"] is not True
        or judgment["candidate_bindings"] != expected_bindings
        or set(judgment["candidate_bindings"]) != set(by_choice)
    ):
        raise SlotRejected("adjudicator did not uniquely recover every hidden binding")
    return {
        "provider": "gemini",
        "role": role_id,
        "model": model_id,
        "judgment_sha256": sha256_hex(canonical_json_bytes(judgment)),
    }


def _adjudicator_candidates(
    *, item: dict[str, Any], secret: str, adjudicator_role: str
) -> list[dict[str, str]]:
    candidates = sorted(
        _role_candidates(item),
        key=lambda row: _keyed_digest(secret, adjudicator_role, row["role"], row["text"]),
    )
    return [
        {
            **row,
            "choice": _keyed_digest(secret, "adjudicator-choice", adjudicator_role, str(index))[
                :24
            ],
        }
        for index, row in enumerate(candidates)
    ]


def _review_choices(*, item: dict[str, Any], secret: str) -> list[dict[str, str]]:
    candidates = _role_candidates(item)
    ordered = sorted(
        candidates,
        key=lambda row: _keyed_digest(secret, "owner", row["role"], row["text"]),
    )
    return [
        {
            **row,
            "choice": _keyed_digest(secret, "choice", item["definition_sha256"], str(index))[:24],
        }
        for index, row in enumerate(ordered)
    ]


def _role_candidates(item: dict[str, Any]) -> list[dict[str, str]]:
    contexts = {row["binding"]: row["content"] for row in item["context_memories"]}
    return [
        {"choice": "A", "role": "target", "text": item["reference_lesson"]},
        {"choice": "B", "role": "unsafe", "text": contexts["unsafe"]},
        {"choice": "C", "role": "ineffective", "text": contexts["ineffective"]},
        {"choice": "D", "role": "background", "text": contexts["none"]},
    ]


def _release_item(*, item: dict[str, Any], split: str, public_id: str) -> dict[str, Any]:
    return {
        "variant_id": public_id,
        "split": split,
        "simulator_kind": item["simulator_kind"],
        "recurrence_query": item["recurrence_query"],
        "source_summary": item["source_summary"],
        "root_cause": item["root_cause"],
        "resolution_action": item["resolution_action"],
        "resolution_observation": item["resolution_observation"],
        "reference_lesson": item["reference_lesson"],
        "reference_source": item["reference_source"],
        "context_memories": [
            {
                "context_id": sha256_hex(f"{public_id}\x1f{index}".encode()),
                "role": row["role"],
                "content": row["content"],
            }
            for index, row in enumerate(item["context_memories"], start=1)
        ],
    }


def _validate_released_item(item: dict[str, Any], *, split: str) -> None:
    required = {
        "variant_id",
        "split",
        "simulator_kind",
        "recurrence_query",
        "source_summary",
        "root_cause",
        "resolution_action",
        "resolution_observation",
        "reference_lesson",
        "reference_source",
        "context_memories",
    }
    if set(item) != required or item.get("split") != split:
        raise ValueError("released v4 item has an invalid study contract")
    if item.get("simulator_kind") not in SIMULATOR_KINDS:
        raise ValueError("released v4 item has an unsupported simulator")
    text_fields = required - {"context_memories"}
    if any(not isinstance(item[field], str) or not item[field].strip() for field in text_fields):
        raise ValueError("released v4 item has empty evidence")
    if item["reference_source"] != REFERENCE_SOURCE:
        raise ValueError("released v4 item has an unsupported reference source")
    evidence = "\n".join(
        item[field]
        for field in (
            "source_summary",
            "root_cause",
            "resolution_action",
            "resolution_observation",
        )
    )
    if _normalize_text(item["reference_lesson"]) in _normalize_text(evidence):
        raise ValueError("released source evidence copies the reference lesson")
    contexts = item["context_memories"]
    if not isinstance(contexts, list) or len(contexts) != 3:
        raise ValueError("released v4 item requires exactly three context memories")
    context_ids = [str(row.get("context_id") or "") for row in contexts]
    if any(not value for value in context_ids) or len(context_ids) != len(set(context_ids)):
        raise ValueError("released v4 context identities must be nonempty and unique")


def _validate_pool(pool: dict[str, Any]) -> None:
    if (
        pool.get("schema_version") != SCHEMA_VERSION
        or pool.get("status") != "accepted"
        or pool.get("terminal_reason") is not None
        or pool.get("protocol_sha256") != protocol_sha256()
        or not isinstance(pool.get("items"), list)
    ):
        raise ValueError("invalid v4 construction pool")
    expected = len(SIMULATOR_KINDS) * ACCEPTED_PER_FAMILY
    counts = Counter(item.get("simulator_kind") for item in pool["items"])
    if len(pool["items"]) != expected or counts != Counter(
        {kind: ACCEPTED_PER_FAMILY for kind in SIMULATOR_KINDS}
    ):
        raise ValueError("v4 construction pool is not balanced")
    without_digest = {key: value for key, value in pool.items() if key != "pool_sha256"}
    if pool.get("pool_sha256") != sha256_hex(canonical_json_bytes(without_digest)):
        raise ValueError("v4 construction pool digest differs from its content")


def _validate_review_binding(*, packet: dict[str, Any], state: dict[str, Any]) -> None:
    if (
        state.get("pool_sha256") != packet.get("pool_sha256")
        or state.get("review_packet_sha256") != packet.get("review_packet_sha256")
        or not isinstance(state.get("decisions"), list)
    ):
        raise ValueError("owner review state differs from its frozen packet")


def _single_action_result(simulator_kind: str, action: str) -> dict[str, Any]:
    simulator = IncidentSimulator(simulator_kind)
    return simulator.step(action)


def _slot_key(*, pool_id: str, simulator_kind: str, slot: int) -> str:
    return "\x1f".join(("hindsight-v4-slot-v1", pool_id, simulator_kind, str(slot)))


def _keyed_digest(secret: str, *parts: str) -> str:
    return hmac.new(secret.encode(), "\x1f".join(parts).encode(), hashlib.sha256).hexdigest()


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        raise SlotRejected("draft fields must be strings")
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _lexical_overlap(left: str, right: str) -> float:
    left_tokens = set(_TOKEN_RE.findall(left.lower()))
    right_tokens = set(_TOKEN_RE.findall(right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _item_normalized_text(item: dict[str, Any]) -> set[str]:
    return {
        _normalize_text(item["source_summary"]),
        _normalize_text(item["root_cause"]),
        _normalize_text(item["resolution_action"]),
        _normalize_text(item["resolution_observation"]),
        _normalize_text(item["recurrence_query"]),
        _normalize_text(item["reference_lesson"]),
        *(_normalize_text(row["content"]) for row in item["context_memories"]),
    }


def _v3_normalized_text(corpus: dict[str, Any]) -> set[str]:
    result = set()
    for row in corpus.get("variants") or []:
        for field in (
            "source_summary",
            "root_cause",
            "resolution_action",
            "resolution_observation",
            "recurrence_query",
            "reference_lesson",
        ):
            if row.get(field):
                result.add(_normalize_text(row[field]))
        for context in row.get("context_memories") or []:
            if context.get("content"):
                result.add(_normalize_text(context["content"]))
    return result
