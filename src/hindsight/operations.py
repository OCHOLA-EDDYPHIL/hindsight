"""Durable, previewed governed-memory correction operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect, database_url
from hindsight.embeddings import EmbeddingProvider
from hindsight.memory import MemoryStore, Provenance
from hindsight.security import safe_error_detail

OperationType = Literal["rewind", "retraction", "supersession", "review_resolution"]
SupersessionIntent = Literal["correction", "evolution"]
ReviewAction = Literal["confirmed", "retracted"]
PREVIEW_TTL = timedelta(minutes=15)


class OperationConflictError(RuntimeError):
    """Raised when approved state no longer matches current governed state."""


class OperationAuthorizationError(PermissionError):
    """Raised when cross-namespace authority is incomplete."""


def preview_rewind(
    *,
    namespace: str,
    target_timestamp: datetime,
    actor: str,
    reason: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Persist an immutable exact-logical-rewind preview."""

    resolved_url = db_url or database_url()
    with MemoryStore(url=resolved_url) as store:
        target = store._semantic_beliefs_as_of(  # noqa: SLF001 - governed sibling layer
            namespace=namespace,
            as_of=target_timestamp,
            limit=None,
            query=None,
        )

    def build(cur: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        current = _current_semantic(cur, namespace=namespace)
        effect = _rewind_effect(target=target, current=current)
        effect["review_resolutions"] = _review_resolutions(
            cur, memory_ids=effect["close_memory_ids"], status="superseded"
        )
        return (
            {
                "namespace": namespace,
                "target_timestamp": _utc(target_timestamp).isoformat(),
                "reason": reason,
            },
            effect,
        )

    return _persist_preview(
        operation_type="rewind",
        actor=actor,
        lock_namespaces=[namespace],
        build=build,
        db_url=resolved_url,
    )


def preview_retraction(
    *,
    root_memory_id: str,
    actor: str,
    reason: str,
    authorized_namespaces: list[str],
    db_url: str | None = None,
) -> dict[str, Any]:
    """Persist a strict cross-namespace causal retraction preview."""

    resolved_url = db_url or database_url()

    def build(cur: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        closure = _causal_closure_with_cursor(cur, root_memory_id=root_memory_id)
        if not closure:
            raise LookupError(f"semantic memory not found: {root_memory_id}")
        _require_complete_lineage(closure)
        _require_authorized(closure, authorized_namespaces)
        close_ids = [str(row["id"]) for row in closure]
        return (
            {
                "root_memory_id": root_memory_id,
                "namespace": str(closure[0]["namespace"]),
                "reason": reason,
                "authorized_namespaces": sorted(set(authorized_namespaces)),
            },
            {
                "close_memory_ids": close_ids,
                "review_resolutions": _review_resolutions(
                    cur, memory_ids=close_ids, status="superseded"
                ),
            },
        )

    return _persist_preview(
        operation_type="retraction",
        actor=actor,
        lock_namespaces=authorized_namespaces,
        build=build,
        db_url=resolved_url,
    )


def preview_supersession(
    *,
    root_memory_id: str,
    intent: SupersessionIntent,
    content: str,
    structured_payload: dict[str, Any],
    actor: str,
    reason: str,
    authorized_namespaces: list[str],
    db_url: str | None = None,
) -> dict[str, Any]:
    """Persist typed correction/evolution supersession impact."""

    if intent not in {"correction", "evolution"}:
        raise ValueError(f"unsupported supersession intent: {intent}")
    resolved_url = db_url or database_url()

    def build(cur: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        closure = _causal_closure_with_cursor(cur, root_memory_id=root_memory_id)
        if not closure:
            raise LookupError(f"semantic memory not found: {root_memory_id}")
        _require_complete_lineage(closure)
        _require_authorized(closure, authorized_namespaces)
        root = closure[0]
        descendants = [row for row in closure if str(row["id"]) != root_memory_id]
        close_ids = (
            [str(row["id"]) for row in closure]
            if intent == "correction"
            else [root_memory_id]
        )
        return (
            {
                "root_memory_id": root_memory_id,
                "namespace": str(root["namespace"]),
                "intent": intent,
                "reason": reason,
                "authorized_namespaces": sorted(set(authorized_namespaces)),
            },
            {
                "close_memory_ids": close_ids,
                "review_memory_ids": [str(row["id"]) for row in descendants]
                if intent == "evolution"
                else [],
                "review_resolutions": _review_resolutions(
                    cur, memory_ids=close_ids, status="superseded"
                ),
                "supersede": {
                    "source_memory_id": root_memory_id,
                    "belief_id": str(root["belief_id"]),
                    "previous_version_id": root_memory_id,
                    "content": content,
                    "structured_payload": structured_payload,
                    "intent": intent,
                },
            },
        )

    return _persist_preview(
        operation_type="supersession",
        actor=actor,
        lock_namespaces=authorized_namespaces,
        build=build,
        db_url=resolved_url,
    )


def preview_review_resolution(
    *,
    review_item_id: str,
    action: ReviewAction,
    actor: str,
    reason: str,
    authorized_namespaces: list[str],
    db_url: str | None = None,
) -> dict[str, Any]:
    """Preview confirming or strictly retracting a quarantined descendant."""

    if action not in {"confirmed", "retracted"}:
        raise ValueError(f"unsupported review action: {action}")
    resolved_url = db_url or database_url()

    def build(cur: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        cur.execute(
            """
                SELECT item.*, memory.namespace, memory.lineage_status
                     , anchor.result_memory_id AS anchor_memory_id
                     , anchor.namespace AS anchor_namespace
                FROM memory_review_items AS item
                JOIN semantic_memories AS memory ON memory.id = item.semantic_memory_id
                LEFT JOIN memory_operation_effects AS anchor
                    ON anchor.operation_id = item.operation_id
                    AND anchor.effect_type = 'created'
                WHERE item.id = %s AND item.status = 'open'
            """,
            (review_item_id,),
        )
        item = cur.fetchone()
        if item is None:
            raise LookupError(f"open review item not found: {review_item_id}")
        reviewed_memory_id = str(item["semantic_memory_id"])
        closure = _causal_closure_with_cursor(
            cur, root_memory_id=reviewed_memory_id
        )
        if not closure or str(closure[0]["id"]) != reviewed_memory_id:
            raise OperationConflictError("reviewed memory is no longer current")
        _require_complete_lineage(closure)
        if action == "confirmed" and item["anchor_memory_id"] is None:
            raise OperationConflictError("evolution anchor version is missing")
        affected = (
            closure
            if action == "retracted"
            else [dict(item), {"namespace": item["anchor_namespace"]}]
        )
        _require_authorized(affected, authorized_namespaces)
        close_ids = (
            [str(row["id"]) for row in closure]
            if action == "retracted"
            else [reviewed_memory_id]
        )
        resolutions = _review_resolutions(
            cur,
            memory_ids=close_ids,
            status="retracted" if action == "retracted" else "superseded",
        )
        if action == "confirmed":
            for resolution in resolutions:
                if resolution["id"] == review_item_id:
                    resolution["status"] = "confirmed"
        return (
            {
                "review_item_id": review_item_id,
                "namespace": str(item["namespace"]),
                "action": action,
                "reason": reason,
                "authorized_namespaces": sorted(set(authorized_namespaces)),
            },
            {
                "semantic_memory_id": reviewed_memory_id,
                "anchor_memory_id": str(item["anchor_memory_id"])
                if item["anchor_memory_id"]
                else None,
                "close_memory_ids": close_ids,
                "review_resolutions": resolutions,
            },
        )

    return _persist_preview(
        operation_type="review_resolution",
        actor=actor,
        lock_namespaces=authorized_namespaces,
        build=build,
        db_url=resolved_url,
    )


def enqueue_operation(
    *,
    preview_id: str,
    fingerprint: str,
    idempotency_key: str,
    db_url: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create one queued operation from an approved immutable preview."""

    if not idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    resolved_url = db_url or database_url()
    with connect(resolved_url, application_name="hindsight-api") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM memory_operations WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if str(existing["preview_id"]) != preview_id or str(
                        existing["preview_fingerprint"]
                    ) != fingerprint:
                        raise OperationConflictError(
                            "idempotency key is already bound to another approved preview"
                        )
                    return dict(existing), False
                cur.execute(
                    "SELECT * FROM memory_operation_previews WHERE id = %s",
                    (preview_id,),
                )
                preview = cur.fetchone()
                if preview is None:
                    raise LookupError(f"preview not found: {preview_id}")
                if preview["expires_at"] <= datetime.now(UTC):
                    raise OperationConflictError("preview has expired")
                if str(preview["fingerprint"]) != fingerprint:
                    raise OperationConflictError("preview fingerprint does not match")
                request = dict(preview["request_payload"])
                operation_id = str(uuid4())
                cur.execute(
                    """
                        INSERT INTO memory_operations (
                            id, operation_type, actor, reason, target_timestamp,
                            namespace, idempotency_key, status, preview_id,
                            preview_fingerprint, root_memory_kind, root_memory_id,
                            expected_revisions, request_payload, attempt_count
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s,
                                %s, %s, %s, %s, %s, 0)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING *
                    """,
                    (
                        operation_id,
                        preview["operation_type"],
                        preview["actor"],
                        request.get("reason") or "Approved governed-memory operation",
                        request.get("target_timestamp"),
                        request.get("namespace"),
                        idempotency_key,
                        preview_id,
                        fingerprint,
                        "semantic" if request.get("root_memory_id") else None,
                        request.get("root_memory_id"),
                        Jsonb(dict(preview["expected_revisions"])),
                        Jsonb(request),
                    ),
                )
                inserted = cur.fetchone()
                if inserted is None:
                    cur.execute(
                        "SELECT * FROM memory_operations WHERE idempotency_key = %s",
                        (idempotency_key,),
                    )
                    raced = cur.fetchone()
                    if raced is None:
                        raise RuntimeError("idempotent operation insert lost without a winner")
                    if str(raced["preview_id"]) != preview_id or str(
                        raced["preview_fingerprint"]
                    ) != fingerprint:
                        raise OperationConflictError(
                            "idempotency key is already bound to another approved preview"
                        )
                    return dict(raced), False
                operation = dict(inserted)
                _append_event(cur, operation_id, "queued", "Memory operation queued")
                return operation, True


def get_operation(*, operation_id: str, db_url: str | None = None) -> dict[str, Any] | None:
    """Return an operation with ordered state/effect records."""

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM memory_operations WHERE id = %s", (operation_id,))
            row = cur.fetchone()
            if row is None:
                return None
            operation = dict(row)
            cur.execute(
                "SELECT * FROM memory_operation_events WHERE operation_id = %s ORDER BY sequence",
                (operation_id,),
            )
            operation["events"] = [dict(item) for item in cur.fetchall()]
            cur.execute(
                "SELECT * FROM memory_operation_effects WHERE operation_id = %s ORDER BY sequence",
                (operation_id,),
            )
            operation["effects"] = [dict(item) for item in cur.fetchall()]
            return operation


def execute_operation(
    *,
    operation_id: str,
    embedding_provider: EmbeddingProvider,
    worker_id: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Lease, verify, and atomically apply one queued memory operation."""

    resolved_url = db_url or database_url()
    operation, preview = _load_for_execution(operation_id=operation_id, db_url=resolved_url)
    if operation["status"] in {"completed", "conflict", "failed"}:
        return operation
    prepared = _precompute_embeddings(
        preview=preview, provider=embedding_provider, db_url=resolved_url
    )
    with connect(resolved_url, application_name="hindsight-memory-worker") as conn:
        try:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                            UPDATE memory_operations
                            SET status = 'leased', lease_owner = %s,
                                lease_expires_at = now() + INTERVAL '2 minutes',
                                attempt_count = attempt_count + 1
                            WHERE id = %s
                                AND status IN ('queued', 'retrying', 'leased')
                                AND (lease_expires_at IS NULL OR lease_expires_at < now()
                                     OR lease_owner = %s)
                            RETURNING *
                        """,
                        (worker_id, operation_id, worker_id),
                    )
                    leased = cur.fetchone()
                    if leased is None:
                        current = get_operation(operation_id=operation_id, db_url=resolved_url)
                        if current is None:
                            raise LookupError(operation_id)
                        return current
                    _append_event(cur, operation_id, "leased", "Memory operation leased")
                    _verify_preview(cur=cur, operation=dict(leased), preview=preview)
                    store = MemoryStore(conn=conn, embedding_provider=embedding_provider)
                    if leased["operation_type"] == "rewind":
                        effects = _apply_rewind(
                            store=store,
                            operation=dict(leased),
                            preview=preview,
                            embeddings=prepared,
                        )
                    elif leased["operation_type"] == "retraction":
                        effects = _apply_retraction(
                            store=store, operation=dict(leased), preview=preview
                        )
                    elif leased["operation_type"] == "supersession":
                        effects = _apply_supersession(
                            store=store,
                            operation=dict(leased),
                            preview=preview,
                            embeddings=prepared,
                        )
                    elif leased["operation_type"] == "review_resolution":
                        effects = _apply_review_resolution(
                            store=store,
                            operation=dict(leased),
                            preview=preview,
                            embeddings=prepared,
                        )
                    else:
                        raise ValueError(f"unsupported operation type: {leased['operation_type']}")
                    _apply_review_resolutions(
                        cur,
                        operation_id=operation_id,
                        resolutions=preview["effect_payload"].get(
                            "review_resolutions", []
                        ),
                    )
                    revisions = _revision_map(cur, list(preview["expected_revisions"]))
                    invalidated = [
                        effect["source_memory_id"]
                        for effect in effects
                        if effect["effect_type"] == "closed"
                    ]
                    restored = [
                        effect["result_memory_id"]
                        for effect in effects
                        if effect["effect_type"] in {"created", "reasserted"}
                    ]
                    cur.execute(
                        """
                            UPDATE memory_operations
                            SET status = 'completed', completed_at = now(),
                                lease_expires_at = NULL, applied_revisions = %s,
                                invalidated_memory_ids = %s, restored_memory_ids = %s
                            WHERE id = %s
                        """,
                        (Jsonb(revisions), Jsonb(invalidated), Jsonb(restored), operation_id),
                    )
                    for sequence, effect in enumerate(effects, start=1):
                        cur.execute(
                            """
                                INSERT INTO memory_operation_effects (
                                    operation_id, sequence, effect_type,
                                    source_memory_id, result_memory_id, belief_id,
                                    namespace, metadata
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                operation_id,
                                sequence,
                                effect["effect_type"],
                                effect.get("source_memory_id"),
                                effect.get("result_memory_id"),
                                effect.get("belief_id"),
                                effect.get("namespace"),
                                Jsonb(effect.get("metadata") or {}),
                            ),
                        )
                    _append_event(cur, operation_id, "completed", "Memory operation completed")
        except OperationConflictError as exc:
            _mark_terminal(
                operation_id=operation_id,
                status="conflict",
                code="stale_preview",
                detail=str(exc),
                db_url=resolved_url,
            )
        except Exception as exc:
            _mark_retry(operation_id=operation_id, exc=exc, db_url=resolved_url)
            raise
    result = get_operation(operation_id=operation_id, db_url=resolved_url)
    if result is None:
        raise LookupError(operation_id)
    return result


def _persist_preview(
    *,
    operation_type: OperationType,
    actor: str,
    lock_namespaces: list[str],
    build: Callable[[Any], tuple[dict[str, Any], dict[str, Any]]],
    db_url: str,
) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                namespaces = sorted(set(lock_namespaces))
                _ensure_namespace_rows(cur, namespaces)
                revisions = _revision_map(cur, namespaces)
                request, effect = build(cur)
                affected_namespaces = _effect_namespaces(cur, effect)
                unlocked = affected_namespaces - set(revisions)
                if unlocked:
                    raise OperationConflictError(
                        "preview effect includes unlocked namespace state: "
                        + ", ".join(sorted(unlocked))
                    )
                cur.execute(
                    "SELECT generation FROM embedding_index_state WHERE singleton = true"
                )
                state = cur.fetchone()
                generation = int(state["generation"]) if state is not None else None
                payload = {
                    "operation_type": operation_type,
                    "actor": actor,
                    "request": request,
                    "effect": effect,
                    "expected_revisions": revisions,
                    "embedding_generation": generation,
                }
                fingerprint = _digest(payload)
                preview_id = str(uuid4())
                cur.execute(
                    """
                        INSERT INTO memory_operation_previews (
                            id, operation_type, actor, request_payload,
                            effect_payload, expected_revisions,
                            embedding_generation, fingerprint, expires_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                    """,
                    (
                        preview_id,
                        operation_type,
                        actor,
                        Jsonb(request),
                        Jsonb(effect),
                        Jsonb(revisions),
                        generation,
                        fingerprint,
                        datetime.now(UTC) + PREVIEW_TTL,
                    ),
                )
                return dict(cur.fetchone())


def _load_for_execution(*, operation_id: str, db_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with connect(db_url, application_name="hindsight-memory-worker") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT operation.*, preview.request_payload AS preview_request,
                           preview.effect_payload AS preview_effect,
                           preview.expected_revisions AS preview_revisions,
                           preview.embedding_generation, preview.expires_at,
                           preview.fingerprint
                    FROM memory_operations AS operation
                    JOIN memory_operation_previews AS preview ON preview.id = operation.preview_id
                    WHERE operation.id = %s
                """,
                (operation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError(operation_id)
            operation = dict(row)
            preview = {
                "request_payload": dict(row["preview_request"]),
                "effect_payload": dict(row["preview_effect"]),
                "expected_revisions": dict(row["preview_revisions"]),
                "embedding_generation": row["embedding_generation"],
                "expires_at": row["expires_at"],
                "fingerprint": row["fingerprint"],
            }
            return operation, preview


def _verify_preview(*, cur: Any, operation: dict[str, Any], preview: dict[str, Any]) -> None:
    if preview["expires_at"] <= datetime.now(UTC):
        raise OperationConflictError("preview expired before execution")
    if operation["preview_fingerprint"] != preview["fingerprint"]:
        raise OperationConflictError("approved fingerprint changed")
    expected = dict(preview["expected_revisions"])
    current = _revision_map(cur, list(expected))
    if current != {str(key): int(value) for key, value in expected.items()}:
        raise OperationConflictError("namespace revision changed after preview")
    cur.execute("SELECT generation FROM embedding_index_state WHERE singleton = true")
    state = cur.fetchone()
    generation = int(state["generation"]) if state is not None else None
    if generation != preview["embedding_generation"]:
        raise OperationConflictError("embedding generation changed after preview")
    _verify_effect_state(cur=cur, operation=operation, preview=preview)


def _verify_effect_state(
    *, cur: Any, operation: dict[str, Any], preview: dict[str, Any]
) -> None:
    request = preview["request_payload"]
    effect = preview["effect_payload"]
    operation_type = operation["operation_type"]
    if operation_type == "rewind":
        cur.execute(
            "SELECT * FROM semantic_memories WHERE id = ANY(%s)",
            (effect["target_memory_ids"],),
        )
        target = [dict(row) for row in cur.fetchall()]
        current = _current_semantic(cur, namespace=request["namespace"])
        rebuilt = _rewind_effect(target=target, current=current)
        if (
            rebuilt["close_memory_ids"] != effect["close_memory_ids"]
            or rebuilt["reassertions"] != effect["reassertions"]
        ):
            raise OperationConflictError("rewind effect changed after preview")
    elif operation_type in {"retraction", "supersession"}:
        closure = _causal_closure_with_cursor(
            cur, root_memory_id=request["root_memory_id"]
        )
        _require_complete_lineage(closure)
        _require_authorized(closure, request["authorized_namespaces"])
        closure_ids = [str(row["id"]) for row in closure]
        if operation_type == "retraction" or request.get("intent") == "correction":
            if closure_ids != effect["close_memory_ids"]:
                raise OperationConflictError("causal closure changed after preview")
        else:
            descendant_ids = [
                value for value in closure_ids if value != request["root_memory_id"]
            ]
            if descendant_ids != effect["review_memory_ids"]:
                raise OperationConflictError("evolution descendants changed after preview")
    elif operation_type == "review_resolution":
        if request["action"] == "retracted":
            closure = _causal_closure_with_cursor(
                cur, root_memory_id=effect["semantic_memory_id"]
            )
            _require_complete_lineage(closure)
            _require_authorized(closure, request["authorized_namespaces"])
            if [str(row["id"]) for row in closure] != effect["close_memory_ids"]:
                raise OperationConflictError("review closure changed after preview")
    expected_reviews = sorted(
        effect.get("review_resolutions", []), key=lambda row: row["id"]
    )
    current_reviews = _review_resolutions(
        cur, memory_ids=effect.get("close_memory_ids", []), status="open"
    )
    current_by_id = {row["id"]: row for row in current_reviews}
    if set(current_by_id) != {row["id"] for row in expected_reviews}:
        raise OperationConflictError("open review items changed after preview")
    if any(
        current_by_id[row["id"]]["semantic_memory_id"]
        != row["semantic_memory_id"]
        for row in expected_reviews
    ):
        raise OperationConflictError("review item target changed after preview")


def _apply_rewind(
    *, store: MemoryStore, operation: dict[str, Any], preview: dict[str, Any], embeddings: dict[str, list[float]]
) -> list[dict[str, Any]]:
    effect = preview["effect_payload"]
    results = _close_memories(store, operation, effect["close_memory_ids"])
    for item in effect["reassertions"]:
        source = store.audit_memory(
            memory_kind="semantic", memory_id=item["source_memory_id"]
        )
        if source is None:
            raise OperationConflictError("rewind source disappeared")
        decision_id = f"operation:{operation['id']}:reassert:{source['id']}"
        store.open_decision(
            decision_id=decision_id,
            actor=operation["actor"],
            decision_kind="rewind_reassertion",
            purpose=operation["reason"],
            namespace=source["namespace"],
        )
        store.record_read(
            decision_id=decision_id,
            memory_kind="semantic",
            memory_id=str(source["id"]),
            reader=operation["actor"],
            purpose="Reassert exact target logical belief",
        )
        created = store.write_semantic(
            namespace=source["namespace"],
            content=source["content"],
            provenance=Provenance(
                writer=operation["actor"],
                source_ref=f"memory:{source['id']}",
                justification=operation["reason"],
            ),
            metadata=dict(source.get("metadata") or {}),
            content_schema=source["content_schema"],
            structured_payload=dict(source["structured_payload"]),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(source["id"])],
            belief_id=str(source["belief_id"]),
            previous_version_id=item["previous_version_id"],
            transition_kind="rewind_reassertion",
            created_by_operation_id=str(operation["id"]),
            precomputed_embedding=embeddings[str(source["id"])],
        )
        results.append(
            {
                "effect_type": "reasserted",
                "source_memory_id": str(source["id"]),
                "result_memory_id": str(created["id"]),
                "belief_id": str(created["belief_id"]),
                "namespace": created["namespace"],
            }
        )
    return results


def _apply_retraction(
    *, store: MemoryStore, operation: dict[str, Any], preview: dict[str, Any]
) -> list[dict[str, Any]]:
    return _close_memories(store, operation, preview["effect_payload"]["close_memory_ids"])


def _apply_supersession(
    *, store: MemoryStore, operation: dict[str, Any], preview: dict[str, Any], embeddings: dict[str, list[float]]
) -> list[dict[str, Any]]:
    effect = preview["effect_payload"]
    results = _close_memories(store, operation, effect["close_memory_ids"])
    item = effect["supersede"]
    source = store.audit_memory(memory_kind="semantic", memory_id=item["source_memory_id"])
    if source is None:
        raise OperationConflictError("supersession source disappeared")
    decision_id = f"operation:{operation['id']}:supersede"
    store.open_decision(
        decision_id=decision_id,
        actor=operation["actor"],
        decision_kind=f"{item['intent']}_supersession",
        purpose=operation["reason"],
        namespace=source["namespace"],
    )
    store.record_read(
        decision_id=decision_id,
        memory_kind="semantic",
        memory_id=str(source["id"]),
        reader=operation["actor"],
        purpose="Supersede an explicitly reviewed belief version",
    )
    created = store.write_semantic(
        namespace=source["namespace"],
        content=item["content"],
        provenance=Provenance(
            writer=operation["actor"],
            source_ref=f"memory:{source['id']}",
            justification=operation["reason"],
        ),
        metadata={**dict(source.get("metadata") or {}), "supersession_intent": item["intent"]},
        content_schema="semantic.supersession.v1",
        structured_payload=dict(item["structured_payload"]),
        producer_decision_id=decision_id,
        parent_memory_ids=[str(source["id"])],
        belief_id=str(source["belief_id"]),
        previous_version_id=str(source["id"]),
        transition_kind="supersession",
        created_by_operation_id=str(operation["id"]),
        precomputed_embedding=embeddings["supersession"],
    )
    results.append(
        {
            "effect_type": "created",
            "source_memory_id": str(source["id"]),
            "result_memory_id": str(created["id"]),
            "belief_id": str(created["belief_id"]),
            "namespace": created["namespace"],
            "metadata": {"intent": item["intent"]},
        }
    )
    for memory_id in effect["review_memory_ids"]:
        row = store._fetch_one(  # noqa: SLF001 - same governed transaction
            """
                UPDATE semantic_memories
                SET trust_status = 'review_required'
                WHERE id = %s AND t_invalid IS NULL
                RETURNING *
            """,
            (memory_id,),
        )
        store._conn.execute(  # noqa: SLF001
            """
                INSERT INTO memory_review_items (
                    operation_id, semantic_memory_id, reason
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (operation_id, semantic_memory_id) DO NOTHING
            """,
            (operation["id"], memory_id, "Ancestor evolved; causal descendant requires review"),
        )
        store._conn.execute(  # noqa: SLF001
            """
                UPDATE memory_namespaces
                SET revision = revision + 1, updated_at = now()
                WHERE namespace = %s
            """,
            (row["namespace"],),
        )
        results.append(
            {
                "effect_type": "review_required",
                "source_memory_id": memory_id,
                "belief_id": str(row["belief_id"]),
                "namespace": row["namespace"],
            }
        )
    return results


def _apply_review_resolution(
    *,
    store: MemoryStore,
    operation: dict[str, Any],
    preview: dict[str, Any],
    embeddings: dict[str, list[float]],
) -> list[dict[str, Any]]:
    request = preview["request_payload"]
    effect = preview["effect_payload"]
    if request["action"] == "retracted":
        results = _close_memories(store, operation, effect["close_memory_ids"])
    else:
        source = store.audit_memory(
            memory_kind="semantic", memory_id=effect["semantic_memory_id"]
        )
        anchor = store.audit_memory(
            memory_kind="semantic", memory_id=effect["anchor_memory_id"]
        )
        if source is None or source["t_invalid"] is not None:
            raise OperationConflictError("reviewed descendant is no longer current")
        if anchor is None or anchor["t_invalid"] is not None:
            raise OperationConflictError("evolution anchor is no longer current")
        decision_id = f"operation:{operation['id']}:review-confirmation"
        store.open_decision(
            decision_id=decision_id,
            actor=operation["actor"],
            decision_kind="review_confirmation",
            purpose=operation["reason"],
            namespace=source["namespace"],
        )
        for memory, purpose in (
            (source, "Revalidate the quarantined descendant"),
            (anchor, "Validate the descendant against the evolved ancestor"),
        ):
            store.record_read(
                decision_id=decision_id,
                memory_kind="semantic",
                memory_id=str(memory["id"]),
                reader=operation["actor"],
                purpose=purpose,
            )
        closed = _close_memories(store, operation, [str(source["id"])])
        created = store.write_semantic(
            namespace=source["namespace"],
            content=source["content"],
            provenance=Provenance(
                writer=operation["actor"],
                source_ref=f"review:{request['review_item_id']}",
                justification=operation["reason"],
            ),
            metadata={**dict(source.get("metadata") or {}), "review_confirmed": True},
            content_schema=source["content_schema"],
            structured_payload=dict(source["structured_payload"]),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(source["id"]), str(anchor["id"])],
            belief_id=str(source["belief_id"]),
            previous_version_id=str(source["id"]),
            transition_kind="supersession",
            created_by_operation_id=str(operation["id"]),
            precomputed_embedding=embeddings["review_confirmation"],
        )
        results = [
            *closed,
            {
                "effect_type": "created",
                "source_memory_id": str(source["id"]),
                "result_memory_id": str(created["id"]),
                "belief_id": str(created["belief_id"]),
                "namespace": created["namespace"],
                "metadata": {
                    "review_status": "confirmed",
                    "anchor_memory_id": str(anchor["id"]),
                },
            }
        ]
    return results


def _apply_review_resolutions(
    cur: Any, *, operation_id: str, resolutions: list[dict[str, str]]
) -> None:
    for resolution in resolutions:
        cur.execute(
            """
                UPDATE memory_review_items
                SET status = %s, resolution_operation_id = %s, resolved_at = now()
                WHERE id = %s AND semantic_memory_id = %s AND status = 'open'
                RETURNING id
            """,
            (
                resolution["status"],
                operation_id,
                resolution["id"],
                resolution["semantic_memory_id"],
            ),
        )
        if cur.fetchone() is None:
            raise OperationConflictError(
                f"review item changed after preview: {resolution['id']}"
            )


def _close_memories(
    store: MemoryStore, operation: dict[str, Any], memory_ids: list[str]
) -> list[dict[str, Any]]:
    results = []
    for memory_id in memory_ids:
        row = store._invalidate_one(  # noqa: SLF001 - same governed transaction
            memory_kind="semantic",
            memory_id=memory_id,
            invalidated_by=operation["actor"],
            reason=operation["reason"],
        )
        if row is None:
            raise OperationConflictError(f"memory is no longer current: {memory_id}")
        results.append(
            {
                "effect_type": "closed",
                "source_memory_id": memory_id,
                "belief_id": str(row["belief_id"]),
                "namespace": row["namespace"],
            }
        )
    return results


def _precompute_embeddings(
    *, preview: dict[str, Any], provider: EmbeddingProvider, db_url: str
) -> dict[str, list[float]]:
    effect = preview["effect_payload"]
    prepared: dict[str, list[float]] = {}
    source_ids = [item["source_memory_id"] for item in effect.get("reassertions", [])]
    if source_ids:
        with connect(db_url, application_name="hindsight-memory-worker") as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id, content FROM semantic_memories WHERE id = ANY(%s)",
                    (source_ids,),
                )
                for row in cur.fetchall():
                    prepared[str(row["id"])] = provider.embed_document(row["content"])
    if effect.get("supersede"):
        prepared["supersession"] = provider.embed_document(effect["supersede"]["content"])
    if (
        preview["request_payload"].get("action") == "confirmed"
        and effect.get("semantic_memory_id")
    ):
        with connect(db_url, application_name="hindsight-memory-worker") as conn:
            row = conn.execute(
                "SELECT content FROM semantic_memories WHERE id = %s",
                (effect["semantic_memory_id"],),
            ).fetchone()
        if row is None:
            raise OperationConflictError("reviewed descendant disappeared")
        prepared["review_confirmation"] = provider.embed_document(str(row[0]))
    return prepared


def _current_semantic(cur: Any, *, namespace: str) -> list[dict[str, Any]]:
    cur.execute(
        """
            SELECT * FROM current_semantic_memories
            WHERE namespace = %s
            ORDER BY t_valid DESC, written_at DESC
        """,
        (namespace,),
    )
    return [dict(row) for row in cur.fetchall()]


def _rewind_effect(
    *, target: list[dict[str, Any]], current: list[dict[str, Any]]
) -> dict[str, Any]:
    target_by_belief = {str(row["belief_id"]): row for row in target}
    current_by_belief = {str(row["belief_id"]): row for row in current}
    close_ids = sorted(
        str(row["id"])
        for belief_id, row in current_by_belief.items()
        if belief_id not in target_by_belief
        or str(target_by_belief[belief_id]["id"]) != str(row["id"])
    )
    reassertions = [
        {
            "source_memory_id": str(row["id"]),
            "belief_id": belief_id,
            "previous_version_id": str(current_by_belief[belief_id]["id"])
            if belief_id in current_by_belief
            else str(row["id"]),
        }
        for belief_id, row in sorted(target_by_belief.items())
        if belief_id not in current_by_belief
        or str(current_by_belief[belief_id]["id"]) != str(row["id"])
    ]
    return {
        "target_memory_ids": sorted(str(row["id"]) for row in target),
        "close_memory_ids": close_ids,
        "reassertions": reassertions,
    }


def _review_resolutions(
    cur: Any, *, memory_ids: list[str], status: str
) -> list[dict[str, str]]:
    if not memory_ids:
        return []
    cur.execute(
        """
            SELECT id, semantic_memory_id
            FROM memory_review_items
            WHERE semantic_memory_id = ANY(%s) AND status = 'open'
            ORDER BY id
        """,
        (memory_ids,),
    )
    return [
        {
            "id": str(row["id"]),
            "semantic_memory_id": str(row["semantic_memory_id"]),
            "status": status,
        }
        for row in cur.fetchall()
    ]


def _effect_namespaces(cur: Any, effect: dict[str, Any]) -> set[str]:
    memory_ids = {
        *[str(value) for value in effect.get("close_memory_ids", [])],
        *[
            str(item["source_memory_id"])
            for item in effect.get("reassertions", [])
        ],
    }
    if effect.get("semantic_memory_id"):
        memory_ids.add(str(effect["semantic_memory_id"]))
    if effect.get("anchor_memory_id"):
        memory_ids.add(str(effect["anchor_memory_id"]))
    if effect.get("supersede"):
        memory_ids.add(str(effect["supersede"]["source_memory_id"]))
    if not memory_ids:
        return set()
    cur.execute(
        "SELECT DISTINCT namespace FROM semantic_memories WHERE id = ANY(%s)",
        (sorted(memory_ids),),
    )
    return {str(row["namespace"]) for row in cur.fetchall()}


def _require_authorized(
    rows: list[dict[str, Any]], authorized_namespaces: list[str]
) -> None:
    missing = {str(row["namespace"]) for row in rows} - set(authorized_namespaces)
    if missing:
        raise OperationAuthorizationError(
            "cross-namespace authorization missing: " + ", ".join(sorted(missing))
        )


def _causal_closure(*, root_memory_id: str, db_url: str) -> list[dict[str, Any]]:
    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _causal_closure_with_cursor(cur, root_memory_id=root_memory_id)


def _causal_closure_with_cursor(
    cur: Any, *, root_memory_id: str
) -> list[dict[str, Any]]:
    cur.execute(
        """
            WITH RECURSIVE closure(id, depth) AS (
                SELECT %s::UUID, 0
                UNION
                SELECT edge.child_semantic_memory_id, closure.depth + 1
                FROM closure
                JOIN memory_reads AS read ON read.semantic_memory_id = closure.id
                JOIN memory_lineage_edges AS edge ON edge.parent_read_id = read.id
                WHERE edge.edge_type IN ('derived', 'reasserted_from')
                    AND edge.child_semantic_memory_id IS NOT NULL
            )
            SELECT memory.*, min(closure.depth) AS causal_depth
            FROM closure
            JOIN semantic_memories AS memory ON memory.id = closure.id
            WHERE memory.t_invalid IS NULL
            GROUP BY memory.id
            ORDER BY causal_depth, memory.written_at, memory.id
        """,
        (root_memory_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def _require_complete_lineage(rows: list[dict[str, Any]]) -> None:
    incomplete = [str(row["id"]) for row in rows if row["lineage_status"] != "complete"]
    if incomplete:
        raise OperationConflictError(
            "strict correction refused incomplete lineage: " + ", ".join(incomplete)
        )


def _ensure_namespace_rows(cur: Any, namespaces: list[str]) -> None:
    for namespace in namespaces:
        cur.execute(
            """
                INSERT INTO memory_namespaces (namespace)
                VALUES (%s)
                ON CONFLICT (namespace) DO NOTHING
            """,
            (namespace,),
        )


def _revision_map(cur: Any, namespaces: list[str]) -> dict[str, int]:
    if not namespaces:
        return {}
    cur.execute(
        "SELECT namespace, revision FROM memory_namespaces WHERE namespace = ANY(%s) FOR UPDATE",
        (namespaces,),
    )
    rows = {str(row["namespace"]): int(row["revision"]) for row in cur.fetchall()}
    missing = set(namespaces) - set(rows)
    if missing:
        raise OperationConflictError("namespace state missing: " + ", ".join(sorted(missing)))
    return rows


def _append_event(cur: Any, operation_id: str, status: str, summary: str) -> None:
    cur.execute(
        """
            INSERT INTO memory_operation_events (
                operation_id, sequence, status, summary
            )
            SELECT %s, COALESCE(max(sequence), 0) + 1, %s, %s
            FROM memory_operation_events
            WHERE operation_id = %s
        """,
        (operation_id, status, summary, operation_id),
    )


def _mark_terminal(
    *, operation_id: str, status: str, code: str, detail: str, db_url: str
) -> None:
    with connect(db_url, application_name="hindsight-memory-worker") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                        UPDATE memory_operations
                        SET status = %s, failure_code = %s, failure_detail = %s,
                            completed_at = now(), lease_expires_at = NULL
                        WHERE id = %s
                    """,
                    (status, code, detail, operation_id),
                )
                _append_event(cur, operation_id, status, detail)


def _mark_retry(*, operation_id: str, exc: Exception, db_url: str) -> None:
    with connect(db_url, application_name="hindsight-memory-worker") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                        UPDATE memory_operations
                        SET status = CASE WHEN attempt_count < 3 THEN 'retrying' ELSE 'failed' END,
                            failure_code = %s, failure_detail = %s,
                            completed_at = CASE WHEN attempt_count < 3 THEN NULL ELSE now() END,
                            lease_expires_at = NULL
                        WHERE id = %s
                        RETURNING status
                    """,
                    (type(exc).__name__, safe_error_detail(exc, max_chars=1000), operation_id),
                )
                row = cur.fetchone()
                status = str(row[0]) if row else "failed"
                _append_event(cur, operation_id, status, "Memory operation attempt failed")


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
