"""API Gateway WebSocket subscriptions and CockroachDB changefeed fanout."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from hindsight.aws import aws_client_config
from hindsight.observability import structured_event
from hindsight.queueing import enqueue_run
from hindsight.realtime_ticket import TICKET_TABLE_ENV, consume_realtime_ticket
from hindsight.security import safe_error_detail
from hindsight.tenant import tenant_lifecycle_fence_key

CONNECTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_CONNECTION_TABLE"
SUBSCRIPTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_SUBSCRIPTION_TABLE"
IDEMPOTENCY_TABLE_ENV = "HINDSIGHT_CHANGEFEED_IDEMPOTENCY_TABLE"
EVENT_LEASE_SECONDS_ENV = "HINDSIGHT_CHANGEFEED_LEASE_SECONDS"
MANAGEMENT_ENDPOINT_ENV = "HINDSIGHT_WEBSOCKET_MANAGEMENT_ENDPOINT"
CHANGEFEED_TOKEN_ENV = "HINDSIGHT_CHANGEFEED_AUTH_TOKEN"
CHANGEFEED_TOKEN_PARAM_ENV = "HINDSIGHT_CHANGEFEED_AUTH_TOKEN_PARAM"
CONNECTION_TTL_SECONDS = 24 * 60 * 60
EVENT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
DEFAULT_EVENT_LEASE_SECONDS = 60
EVENT_VERSION = 2
LEGACY_EVENT_VERSION = 1
_HLC_PATTERN = re.compile(r"^[0-9]+\.[0-9]+$")
_CHANGEFEED_TOKEN_CACHE: str | None = None
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


def _otel_enabled() -> bool:
    return os.environ.get("HINDSIGHT_OTEL_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class OutboxEventClaim:
    """Result of attempting to own one durable outbox projection."""

    status: Literal["claimed", "busy", "completed"]
    owner: str | None = None


class OutboxEventBusy(RuntimeError):
    """The event is still owned by another live changefeed invocation."""


class OutboxEventLeaseLost(RuntimeError):
    """The event lease expired or was taken over before completion."""


def websocket_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle API Gateway WebSocket connect, disconnect, and subscription routes."""

    if _otel_enabled():
        from hindsight.tracing import configure_tracing_from_env, start_span

        configure_tracing_from_env(service_name="hindsight-realtime")
        request_context = event.get("requestContext") or {}
        with start_span(
            "hindsight.realtime.websocket",
            {
                "hindsight.realtime.route": request_context.get("routeKey"),
                "hindsight.realtime.connection_id": request_context.get("connectionId"),
            },
        ):
            return _websocket_handler(event, context)
    return _websocket_handler(event, context)


def _websocket_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:

    request_context = event.get("requestContext") or {}
    route = str(request_context.get("routeKey") or "$default")
    connection_id = str(request_context.get("connectionId") or "").strip()
    if not connection_id:
        return _response(400, {"error": "connection id is required"})
    table_name = os.environ.get(CONNECTION_TABLE_ENV)
    subscription_table_name = os.environ.get(SUBSCRIPTION_TABLE_ENV)
    if not table_name or not subscription_table_name:
        return _response(503, {"error": "connection registry is not configured"})
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )
    table = dynamodb.Table(table_name)
    subscriptions = dynamodb.Table(subscription_table_name)
    try:
        if route == "$connect":
            ticket = str((event.get("queryStringParameters") or {}).get("ticket") or "")
            ticket_table_name = os.environ.get(TICKET_TABLE_ENV)
            if not ticket_table_name:
                return _response(503, {"error": "realtime ticket registry is not configured"})
            try:
                claims = consume_realtime_ticket(
                    ticket,
                    table=dynamodb.Table(ticket_table_name),
                )
            except ValueError as exc:
                return _response(401, {"error": str(exc)})
            connected_at = int(time.time())
            expires_at = min(
                claims.session_expires_at,
                connected_at + CONNECTION_TTL_SECONDS,
            )
            if expires_at <= connected_at:
                return _response(401, {"error": "realtime ticket is invalid or expired"})
            if _tenant_realtime_fenced(table, claims.tenant_id):
                return _response(410, {"error": "tenant realtime access is retired"})
            table.put_item(
                Item={
                    "connection_id": connection_id,
                    "namespace": "",
                    "run_id": "",
                    "tenant_id": claims.tenant_id,
                    "access_class": claims.access_class,
                    "principal_id": claims.principal_id or "",
                    "expires_at": expires_at,
                }
            )
            return _response(200, {"connected": True})
        if route == "$disconnect":
            _delete_subscriptions(subscriptions, connection_id)
            table.delete_item(Key={"connection_id": connection_id})
            return _response(200, {"connected": False})

        connection = table.get_item(
            Key={"connection_id": connection_id},
            ConsistentRead=True,
        ).get("Item")
        if not connection:
            return _response(404, {"error": "connection is not registered"})
        tenant_id = str(connection.get("tenant_id") or "")
        if not tenant_id:
            return _response(403, {"error": "connection tenant is unavailable"})
        current_time = int(time.time())
        try:
            expires_at = int(connection.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at <= current_time:
            _delete_subscriptions(subscriptions, connection_id)
            table.delete_item(Key={"connection_id": connection_id})
            return _response(401, {"error": "connection session has expired"})
        if _tenant_realtime_fenced(table, tenant_id):
            _delete_subscriptions(subscriptions, connection_id)
            table.delete_item(Key={"connection_id": connection_id})
            return _response(410, {"error": "tenant realtime access is retired"})

        payload = _event_body(event)
        message_type = str(payload.get("type") or "").strip().lower()
        if message_type == "ping":
            return _response(200, {"type": "pong"})
        if message_type not in {"subscribe", "unsubscribe"}:
            return _response(400, {"error": "type must be subscribe, unsubscribe, or ping"})
        namespace = _optional_subscription(payload.get("namespace"), "namespace")
        run_id = _optional_subscription(payload.get("run_id"), "run_id")
        if message_type == "unsubscribe":
            namespace = ""
            run_id = ""
        _delete_subscriptions(subscriptions, connection_id)
        if message_type == "subscribe":
            topic_keys = []
            if namespace:
                topic_keys.append(f"tenant:{tenant_id}:namespace:{namespace}")
            if run_id:
                topic_keys.append(f"tenant:{tenant_id}:run:{run_id}")
            for topic_key in topic_keys:
                subscriptions.put_item(
                    Item={
                        "topic_key": topic_key,
                        "connection_id": connection_id,
                        "tenant_id": tenant_id,
                        "expires_at": expires_at,
                    }
                )
        table.update_item(
            Key={"connection_id": connection_id},
            UpdateExpression=(
                "SET #namespace = :namespace, run_id = :run_id, expires_at = :expires_at"
            ),
            ExpressionAttributeNames={"#namespace": "namespace"},
            ExpressionAttributeValues={
                ":namespace": namespace or "",
                ":run_id": run_id or "",
                ":expires_at": expires_at,
            },
            ConditionExpression="attribute_exists(connection_id)",
        )
        return _response(
            200,
            {"subscribed": message_type == "subscribe", "namespace": namespace, "run_id": run_id},
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": str(exc)})
    except Exception as exc:
        return _response(500, {"error": safe_error_detail(exc)})


def _tenant_realtime_fenced(table: Any, tenant_id: str) -> bool:
    response = table.get_item(
        Key={"connection_id": tenant_lifecycle_fence_key(tenant_id)},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return isinstance(item, dict) and item.get("lifecycle_fence") is True


def changefeed_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if _otel_enabled():
        from hindsight.tracing import configure_tracing_from_env, start_span

        configure_tracing_from_env(service_name="hindsight-realtime")
        with start_span("hindsight.realtime.changefeed"):
            return _changefeed_handler(event, context)
    return _changefeed_handler(event, context)


def _changefeed_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Authenticate one webhook batch and fan normalized rows out to subscribers."""

    if not _changefeed_authorized(event):
        return _response(401, {"error": "invalid changefeed authorization"})
    try:
        body = _event_body(event)
        rows = body.get("payload", []) if isinstance(body, dict) else body
        if not isinstance(rows, list):
            raise ValueError("changefeed payload must be a list")
        accepted = 0
        delivered = 0
        stale = 0
        duplicates_ignored = 0
        consolidation_jobs = 0
        for row in rows:
            event_id = _outbox_event_id(row)
            claim: OutboxEventClaim | None = None
            if event_id is not None:
                claim = _claim_outbox_event(event_id)
                if claim.status == "completed":
                    duplicates_ignored += 1
                    continue
                if claim.status == "busy":
                    raise OutboxEventBusy("changefeed event is already being processed")
            try:
                row_consolidation_jobs = 0
                row_accepted = 0
                row_delivered = 0
                row_stale = 0
                transition = _resolved_incident_transition(row)
                if transition is not None:
                    enqueue_run(
                        {
                            "command": "consolidation",
                            "incident_id": transition["id"],
                            "source_event_id": transition["resolution_event_id"],
                            **(
                                {"tenant_id": transition["tenant_id"]}
                                if transition.get("tenant_id")
                                else {}
                            ),
                        }
                    )
                    row_consolidation_jobs = 1
                envelope = normalize_changefeed_row(row)
                if envelope is not None:
                    result = fanout_event(envelope)
                    row_accepted = 1
                    row_delivered = result["delivered"]
                    row_stale = result["stale"]
                    LOGGER.info(
                        structured_event(
                            "realtime_changefeed",
                            {
                                "tenant_id": envelope.get("tenant_id"),
                                "run_id": envelope.get("run_id"),
                                "message_id": envelope.get("event_id"),
                                "status": "delivered",
                            },
                        )
                    )
                if event_id is not None and claim is not None:
                    if claim.owner is None or not _complete_outbox_event(event_id, claim.owner):
                        raise OutboxEventLeaseLost(
                            "changefeed event ownership was lost before completion"
                        )
                consolidation_jobs += row_consolidation_jobs
                accepted += row_accepted
                delivered += row_delivered
                stale += row_stale
            except Exception:
                if event_id is not None and claim is not None and claim.owner is not None:
                    _release_outbox_event(event_id, claim.owner)
                raise
        return _response(
            200,
            {
                "accepted": accepted,
                "delivered": delivered,
                "duplicates_ignored": duplicates_ignored,
                "stale_connections_removed": stale,
                "consolidation_jobs_queued": consolidation_jobs,
            },
        )
    except (OutboxEventBusy, OutboxEventLeaseLost) as exc:
        return _response(
            503,
            {"error": str(exc), "retryable": True},
            headers={"retry-after": "1"},
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": str(exc)})
    except Exception as exc:
        return _response(500, {"error": safe_error_detail(exc)})


def normalize_changefeed_row(row: Any) -> dict[str, Any] | None:
    """Convert one CockroachDB webhook row to the product event envelope."""

    if not isinstance(row, dict) or row.get("resolved"):
        return None
    after = row.get("after")
    if after is None and isinstance(row.get("value"), dict):
        after = row["value"].get("after", row["value"])
    if not isinstance(after, dict):
        return None
    topic = str(row.get("topic") or row.get("table") or "").split(".")[-1]
    updated = (
        row.get("updated")
        or after.get("updated_at")
        or after.get("created_at")
        or after.get("written_at")
        or "0"
    )
    if topic == "tenant_event_outbox":
        aggregate_type = str(after.get("aggregate_type") or "")
        event_type = {
            "semantic_memories": "memory",
            "memory_operations": "operation",
            "agent_runs": "run",
            "agent_run_events": "run_event",
        }.get(aggregate_type)
        if event_type is None:
            return None
        event_id = str(after.get("id") or "")
        tenant_id = str(after.get("tenant_id") or "")
        if not event_id or not tenant_id:
            raise ValueError("outbox event identity is required")
        payload = after.get("payload") if isinstance(after.get("payload"), dict) else {}
        topics = after.get("topics") if isinstance(after.get("topics"), list) else []
        topic_keys = [str(value) for value in topics]
        if any(not value.startswith(f"tenant:{tenant_id}:") for value in topic_keys):
            raise ValueError("outbox topic tenant does not match event tenant")
        hlc = _required_changefeed_hlc(row.get("updated"))
        return {
            "version": EVENT_VERSION,
            "event_id": event_id,
            "cursor": {"hlc": hlc, "event_id": event_id},
            "tenant_id": tenant_id,
            "topic_keys": topic_keys,
            "type": event_type,
            "namespace": None,
            "run_id": str(payload.get("run_id")) if payload.get("run_id") else None,
            "occurred_at": str(
                payload.get("updated_at") or after.get("created_at") or hlc
            ),
            "data": {"reference": _jsonable(payload)},
        }
    namespace = after.get("namespace")
    run_id = after.get("run_id") or (after.get("id") if topic == "agent_runs" else None)

    if topic == "semantic_memories":
        event_type = "memory"
        data = {"memory": _normalize_memory(after)}
    elif topic == "memory_operations":
        event_type = "operation"
        data = {"operation": _normalize_operation(after)}
    elif topic == "agent_runs":
        event_type = "run"
        data = {"run": _jsonable(after)}
    elif topic == "agent_run_events":
        event_type = "run_event"
        data = {"run_event": _jsonable(after)}
    else:
        return None
    return {
        "version": LEGACY_EVENT_VERSION,
        "type": event_type,
        "namespace": namespace,
        "run_id": str(run_id) if run_id is not None else None,
        "occurred_at": str(updated),
        "data": data,
    }


def _resolved_incident_transition(row: Any) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    topic = str(row.get("topic") or row.get("table") or "").split(".")[-1]
    if topic == "tenant_event_outbox":
        after = row.get("after")
        if after is None and isinstance(row.get("value"), dict):
            after = row["value"].get("after", row["value"])
        if not isinstance(after, dict) or after.get("aggregate_type") != "incidents":
            return None
        payload = after.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("previous_status") == "resolved" or payload.get("status") != "resolved":
            return None
        if payload.get("consolidation_policy", "managed") != "managed":
            return None
        if not payload.get("incident_id") or not payload.get("resolution_event_id"):
            return None
        return {
            "id": str(payload["incident_id"]),
            "resolution_event_id": str(payload["resolution_event_id"]),
            "tenant_id": str(after.get("tenant_id") or ""),
        }
    if topic != "incidents":
        return None
    value = row.get("value") if isinstance(row.get("value"), dict) else row
    before = value.get("before") if isinstance(value, dict) else None
    after = value.get("after") if isinstance(value, dict) else None
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if before.get("status") == "resolved" or after.get("status") != "resolved":
        return None
    if after.get("consolidation_policy", "managed") != "managed":
        # Manual producers (including the governed benchmark) own their
        # consolidation lifecycle and must not receive a second async claimant.
        return None
    if not after.get("id") or not after.get("resolution_event_id"):
        return None
    return {
        "id": str(after["id"]),
        "resolution_event_id": str(after["resolution_event_id"]),
    }


def _outbox_event_id(row: Any) -> str | None:
    if not isinstance(row, dict) or row.get("resolved"):
        return None
    topic = str(row.get("topic") or row.get("table") or "").split(".")[-1]
    if topic != "tenant_event_outbox":
        return None
    after = row.get("after")
    if after is None and isinstance(row.get("value"), dict):
        after = row["value"].get("after", row["value"])
    if not isinstance(after, dict) or not after.get("id"):
        raise ValueError("outbox event identity is required")
    return str(after["id"])


def fanout_event(
    envelope: dict[str, Any],
    *,
    table: Any | None = None,
    connection_table: Any | None = None,
    management_client: Any | None = None,
) -> dict[str, int]:
    """Send an event to matching subscriptions and remove HTTP 410 connections."""

    table = table or _subscription_table()
    connection_table = connection_table or _connection_table()
    management_client = management_client or _management_client()
    delivered = 0
    stale = 0
    subscriptions: dict[str, dict[str, Any]] = {}
    for topic_key in envelope.get("topic_keys") or []:
        response = table.query(KeyConditionExpression=Key("topic_key").eq(str(topic_key)))
        for subscription in response.get("Items", []):
            subscriptions[str(subscription["connection_id"])] = subscription
    for connection_id, subscription in subscriptions.items():
        if int(subscription.get("expires_at") or 0) <= int(time.time()):
            table.delete_item(
                Key={
                    "topic_key": subscription["topic_key"],
                    "connection_id": connection_id,
                }
            )
            continue
        try:
            management_client.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
            )
            delivered += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"GoneException", "410"}:
                raise
            _delete_subscriptions(table, connection_id)
            connection_table.delete_item(Key={"connection_id": connection_id})
            stale += 1
    return {"delivered": delivered, "stale": stale}


def _delete_subscriptions(table: Any, connection_id: str) -> None:
    response = table.query(
        IndexName="connection-id-index",
        KeyConditionExpression=Key("connection_id").eq(connection_id),
    )
    for item in response.get("Items", []):
        table.delete_item(
            Key={
                "topic_key": item["topic_key"],
                "connection_id": connection_id,
            }
        )


def _normalize_memory(row: dict[str, Any]) -> dict[str, Any]:
    memory = _jsonable(row)
    memory["status"] = "invalidated" if row.get("t_invalid") is not None else "current"
    return memory


def _normalize_operation(row: dict[str, Any]) -> dict[str, Any]:
    operation = _jsonable(row)
    operation["invalidated_memory_ids"] = row.get("invalidated_memory_ids") or []
    operation["restored_memory_ids"] = row.get("restored_memory_ids") or []
    return operation


def _changefeed_authorized(event: dict[str, Any]) -> bool:
    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    supplied = headers.get("authorization", "")
    expected = _changefeed_token()
    return bool(expected and supplied and hmac.compare_digest(supplied, f"Bearer {expected}"))


def _changefeed_token() -> str | None:
    global _CHANGEFEED_TOKEN_CACHE
    if _CHANGEFEED_TOKEN_CACHE is not None:
        return _CHANGEFEED_TOKEN_CACHE
    direct = os.environ.get(CHANGEFEED_TOKEN_ENV)
    if direct:
        _CHANGEFEED_TOKEN_CACHE = direct
        return direct
    parameter = os.environ.get(CHANGEFEED_TOKEN_PARAM_ENV)
    if not parameter:
        return None
    client = boto3.client(
        "ssm",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )
    response = client.get_parameter(Name=parameter, WithDecryption=True)
    _CHANGEFEED_TOKEN_CACHE = str(response["Parameter"]["Value"])
    return _CHANGEFEED_TOKEN_CACHE


def _connection_table() -> Any:
    table_name = os.environ.get(CONNECTION_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{CONNECTION_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)


def _subscription_table() -> Any:
    table_name = os.environ.get(SUBSCRIPTION_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{SUBSCRIPTION_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)


def _idempotency_table() -> Any:
    table_name = os.environ.get(IDEMPOTENCY_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{IDEMPOTENCY_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)


def _event_lease_seconds() -> int:
    raw = os.environ.get(EVENT_LEASE_SECONDS_ENV, str(DEFAULT_EVENT_LEASE_SECONDS))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{EVENT_LEASE_SECONDS_ENV} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{EVENT_LEASE_SECONDS_ENV} must be greater than zero")
    return value


def _claim_outbox_event(
    event_id: str,
    *,
    table: Any | None = None,
    owner: str | None = None,
    now: int | None = None,
) -> OutboxEventClaim:
    resolved_table = table or _idempotency_table()
    claimed_at = int(time.time()) if now is None else now
    lease_owner = owner or str(uuid4())
    lease_expires_at = claimed_at + _event_lease_seconds()
    try:
        resolved_table.update_item(
            Key={"event_id": event_id},
            UpdateExpression=(
                "SET #state = :processing, lease_owner = :owner, "
                "lease_expires_at = :lease_expires_at, "
                "attempt_count = if_not_exists(attempt_count, :zero) + :one, "
                "started_at = if_not_exists(started_at, :now), "
                "updated_at = :now, expires_at = :expires_at REMOVE completed_at"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":processing": "processing",
                ":owner": lease_owner,
                ":lease_expires_at": lease_expires_at,
                ":zero": 0,
                ":one": 1,
                ":now": claimed_at,
                ":expires_at": claimed_at + EVENT_IDEMPOTENCY_TTL_SECONDS,
            },
            ConditionExpression=(
                "attribute_not_exists(event_id) OR "
                "(#state = :processing AND lease_expires_at <= :now) OR "
                "(attribute_not_exists(#state) AND expires_at <= :now)"
            ),
        )
    except resolved_table.meta.client.exceptions.ConditionalCheckFailedException:
        existing = resolved_table.get_item(
            Key={"event_id": event_id},
            ConsistentRead=True,
        ).get("Item")
        if isinstance(existing, dict) and existing.get("state") == "completed":
            return OutboxEventClaim("completed")
        return OutboxEventClaim("busy")
    return OutboxEventClaim("claimed", lease_owner)


def _complete_outbox_event(
    event_id: str,
    owner: str,
    *,
    table: Any | None = None,
    now: int | None = None,
) -> bool:
    resolved_table = table or _idempotency_table()
    completed_at = int(time.time()) if now is None else now
    try:
        resolved_table.update_item(
            Key={"event_id": event_id},
            UpdateExpression=(
                "SET #state = :completed, completed_at = :now, updated_at = :now, "
                "expires_at = :expires_at REMOVE lease_owner, lease_expires_at"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":processing": "processing",
                ":completed": "completed",
                ":owner": owner,
                ":now": completed_at,
                ":expires_at": completed_at + EVENT_IDEMPOTENCY_TTL_SECONDS,
            },
            ConditionExpression=(
                "#state = :processing AND lease_owner = :owner "
                "AND lease_expires_at > :now"
            ),
        )
    except resolved_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _release_outbox_event(
    event_id: str,
    owner: str,
    *,
    table: Any | None = None,
    now: int | None = None,
) -> bool:
    """Expire only the caller's processing lease so a retry can take over."""

    resolved_table = table or _idempotency_table()
    released_at = int(time.time()) if now is None else now
    try:
        resolved_table.update_item(
            Key={"event_id": event_id},
            UpdateExpression=(
                "SET lease_expires_at = :expired, updated_at = :now, expires_at = :expires_at"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":processing": "processing",
                ":owner": owner,
                ":expired": released_at - 1,
                ":now": released_at,
                ":expires_at": released_at + EVENT_IDEMPOTENCY_TTL_SECONDS,
            },
            ConditionExpression="#state = :processing AND lease_owner = :owner",
        )
    except resolved_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _required_changefeed_hlc(value: Any) -> str:
    hlc = str(value or "").strip()
    if not _HLC_PATTERN.fullmatch(hlc):
        raise ValueError("outbox changefeed row requires a CockroachDB updated HLC")
    return hlc


def _management_client() -> Any:
    endpoint = os.environ.get(MANAGEMENT_ENDPOINT_ENV)
    if not endpoint:
        raise RuntimeError(f"{MANAGEMENT_ENDPOINT_ENV} is required")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.strip("/"):
        raise RuntimeError(
            f"{MANAGEMENT_ENDPOINT_ENV} must be an HTTPS stage endpoint"
        )
    return boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )


def _event_body(event: dict[str, Any]) -> Any:
    body = event.get("body")
    if body is None:
        return {}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    if isinstance(body, (dict, list)):
        return body
    return json.loads(body)


def _optional_subscription(value: Any, name: str) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if len(result) > 500:
        raise ValueError(f"{name} must be at most 500 characters")
    return result or None


def _response(
    status_code: int,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json", **(headers or {})},
        "body": json.dumps(payload, sort_keys=True),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
