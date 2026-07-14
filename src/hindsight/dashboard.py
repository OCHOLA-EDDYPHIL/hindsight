"""Live memory dashboard for the Hindsight demo."""

from __future__ import annotations

import hmac
import json
import os
import queue
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse
from uuid import UUID

from psycopg.rows import dict_row

from hindsight.db import connect, database_url
from hindsight.demo_state import DEMO_NAMESPACE
from hindsight.memory import MemoryStore
from hindsight.security import safe_error_detail

MAX_SNAPSHOT_ROWS = 100
HISTORICAL_SNAPSHOT_TTL_SECONDS = 3.0
BROKER_IDLE_TIMEOUT_SECONDS = 90.0
DASHBOARD_AUTH_TOKEN_ENV = "HINDSIGHT_DASHBOARD_AUTH_TOKEN"
DASHBOARD_AUTH_COOKIE = "hindsight_dashboard_token"


class DashboardServer(ThreadingHTTPServer):
    """HTTP server carrying dashboard configuration."""

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        namespace: str,
        db_url: str | None = None,
        auth_token: str | None = None,
        historical_snapshot_ttl: float = HISTORICAL_SNAPSHOT_TTL_SECONDS,
        broker_idle_timeout: float = BROKER_IDLE_TIMEOUT_SECONDS,
    ):
        super().__init__(server_address, DashboardRequestHandler)
        self.namespace = namespace
        self.db_url = db_url
        self.auth_token = _optional_token(auth_token or os.environ.get(DASHBOARD_AUTH_TOKEN_ENV))
        self.historical_snapshot_ttl = historical_snapshot_ttl
        self.broker_idle_timeout = broker_idle_timeout
        self._brokers: dict[str, DashboardBroker] = {}
        self._broker_lock = threading.Lock()
        self._historical_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
        self._historical_cache_lock = threading.Lock()

    def broker_for(self, namespace: str) -> "DashboardBroker":
        with self._broker_lock:
            self._cleanup_idle_brokers_locked()
            broker = self._brokers.get(namespace)
            if broker is None:
                broker = DashboardBroker(namespace=namespace, db_url=self.db_url)
                self._brokers[namespace] = broker
            return broker

    def current_snapshot(self, *, namespace: str, limit: int = MAX_SNAPSHOT_ROWS) -> dict[str, Any]:
        return self.broker_for(namespace).current_snapshot(limit=limit)

    def historical_snapshot(
        self,
        *,
        namespace: str,
        as_of: str,
        limit: int = MAX_SNAPSHOT_ROWS,
    ) -> dict[str, Any]:
        self.cleanup_idle_brokers()
        timestamp = _parse_timestamp(as_of)
        if timestamp is None:
            raise ValueError("as_of is required")
        cache_key = (namespace, timestamp.isoformat(), limit)
        now = time.monotonic()
        with self._historical_cache_lock:
            cached = self._historical_cache.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]

        snapshot = memory_snapshot(
            namespace=namespace,
            as_of=timestamp,
            db_url=self.db_url,
            limit=limit,
        )
        expires_at = time.monotonic() + self.historical_snapshot_ttl
        with self._historical_cache_lock:
            self._historical_cache[cache_key] = (expires_at, snapshot)
        return snapshot

    def cleanup_idle_brokers(self) -> None:
        with self._broker_lock:
            self._cleanup_idle_brokers_locked()

    def release_subscription(
        self,
        *,
        namespace: str,
        subscription: "DashboardSubscription",
    ) -> None:
        with self._broker_lock:
            broker = self._brokers.get(namespace)
        if broker is not None:
            broker.unsubscribe(subscription)
        self.cleanup_idle_brokers()

    def _cleanup_idle_brokers_locked(self) -> None:
        idle_namespaces = [
            namespace
            for namespace, broker in self._brokers.items()
            if broker.is_idle_for(self.broker_idle_timeout)
        ]
        for namespace in idle_namespaces:
            broker = self._brokers.pop(namespace)
            broker.close()

    def server_close(self) -> None:
        with self._broker_lock:
            brokers = list(self._brokers.values())
            self._brokers.clear()
        for broker in brokers:
            broker.close()
        super().server_close()


@dataclass(frozen=True)
class DashboardSubscription:
    """SSE subscriber registered with a dashboard broker."""

    snapshot: dict[str, Any]
    events: "queue.Queue[dict[str, Any] | None]"


class DashboardBroker:
    """Share one namespace changefeed and cached snapshot across SSE clients."""

    def __init__(
        self,
        *,
        namespace: str,
        db_url: str | None = None,
        snapshot_loader: Callable[..., dict[str, Any]] | None = None,
        changefeed_loader: Callable[..., Iterator[dict[str, Any]]] | None = None,
        cursor_provider: Callable[..., datetime] | None = None,
        limit: int = MAX_SNAPSHOT_ROWS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.namespace = namespace
        self.db_url = db_url
        self._snapshot_loader = snapshot_loader or memory_snapshot
        self._changefeed_loader = changefeed_loader or changefeed_events
        self._cursor_provider = cursor_provider or changefeed_cursor
        self._limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._subscribers: list["queue.Queue[dict[str, Any] | None]"] = []
        self._snapshot: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._startup_error: Exception | None = None
        self._last_used_at = self._clock()
        self._closed = False

    def subscribe(self, *, timeout: float = 30.0) -> DashboardSubscription:
        """Return a cached snapshot and an event queue for one SSE client."""

        self._ensure_ready(timeout=timeout)
        events: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=200)
        with self._lock:
            self._last_used_at = self._clock()
            snapshot = self._snapshot_copy()
            self._subscribers.append(events)
        return DashboardSubscription(snapshot=snapshot, events=events)

    def current_snapshot(
        self,
        *,
        limit: int = MAX_SNAPSHOT_ROWS,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Return the cached current snapshot, starting the broker if needed."""

        if limit != self._limit:
            return self._snapshot_loader(
                namespace=self.namespace,
                db_url=self.db_url,
                limit=limit,
            )
        self._ensure_ready(timeout=timeout)
        with self._lock:
            self._last_used_at = self._clock()
            return self._snapshot_copy()

    def unsubscribe(self, subscription: DashboardSubscription) -> None:
        with self._lock:
            if subscription.events in self._subscribers:
                self._subscribers.remove(subscription.events)
            self._last_used_at = self._clock()

    def is_idle_for(self, idle_timeout: float) -> bool:
        with self._lock:
            return (
                not self._closed
                and not self._subscribers
                and self._clock() - self._last_used_at >= idle_timeout
            )

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            if self._closed:
                raise RuntimeError("dashboard broker is closed")
            self._thread = threading.Thread(
                target=self._run,
                name=f"hindsight-dashboard-{self.namespace}",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._closed = True
            self._last_used_at = self._clock()
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            _put_sse_event(subscriber, None)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        try:
            cursor = self._cursor_provider(db_url=self.db_url)
            with self._lock:
                self._snapshot = self._load_snapshot()
                self._last_used_at = self._clock()
            self._ready.set()
            for event in self._changefeed_loader(
                namespace=self.namespace,
                db_url=self.db_url,
                stop_event=self._stop,
                cursor=cursor,
            ):
                if self._stop.is_set():
                    break
                self._apply_event(event)
                self._publish(event)
        except Exception as exc:  # pragma: no cover - exercised through broker tests
            self._startup_error = exc
            self._ready.set()
            self._publish({"event": "error", "type": "error", "error": safe_error_detail(exc)})

    def _load_snapshot(self) -> dict[str, Any]:
        return self._snapshot_loader(
            namespace=self.namespace,
            db_url=self.db_url,
            limit=self._limit,
        )

    def _apply_event(self, event: dict[str, Any]) -> None:
        if event.get("event") not in {"memory", "operation"}:
            return
        with self._lock:
            snapshot = self._snapshot or self._load_snapshot()
            if event["event"] == "memory":
                snapshot["memories"] = _upsert_recent(
                    snapshot.get("memories", []),
                    event["memory"],
                    sort_key=_memory_event_sort_key,
                    limit=self._limit,
                )
            if event["event"] == "operation":
                snapshot["operations"] = _upsert_recent(
                    snapshot.get("operations", []),
                    event["operation"],
                    sort_key=_operation_event_sort_key,
                    limit=self._limit,
                )
            snapshot["timeline"] = _timeline(snapshot.get("memories", []), snapshot.get("operations", []))
            snapshot["generated_at"] = datetime.now(UTC).isoformat()
            self._snapshot = snapshot

    def _ensure_ready(self, *, timeout: float) -> None:
        self.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("memory dashboard changefeed did not become ready")
        if self._startup_error is not None:
            raise RuntimeError(str(self._startup_error)) from self._startup_error

    def _snapshot_copy(self) -> dict[str, Any]:
        return json.loads(json.dumps(_jsonable(self._snapshot or self._load_snapshot())))

    def _publish(self, event: dict[str, Any]) -> None:
        stale: list[queue.Queue[dict[str, Any] | None]] = []
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            if not _put_sse_event(subscriber, event):
                stale.append(subscriber)
        if stale:
            with self._lock:
                self._subscribers = [subscriber for subscriber in self._subscribers if subscriber not in stale]


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the dashboard page, snapshots, and changefeed-backed SSE stream."""

    server: DashboardServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/" and self._query_token_valid(params):
            self._send_auth_cookie_redirect(parsed)
            return
        if not self._authorized(params):
            self._send_unauthorized()
            return
        if parsed.path == "/":
            self._send_html(dashboard_html(default_namespace=self.server.namespace))
            return
        if parsed.path == "/snapshot":
            self._send_snapshot(parsed.query)
            return
        if parsed.path == "/events":
            self._send_events(parsed.query)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        """Keep demo output quiet except for explicit startup messages."""

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_redirect(self, location: str, *, cookie_token: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("location", location)
        if cookie_token is not None:
            self.send_header(
                "set-cookie",
                (
                    f"{DASHBOARD_AUTH_COOKIE}={quote(cookie_token, safe='')}; "
                    "Path=/; HttpOnly; SameSite=Lax"
                ),
            )
        self.send_header("content-length", "0")
        self.end_headers()

    def _send_auth_cookie_redirect(self, parsed: Any) -> None:
        params = parse_qs(parsed.query, keep_blank_values=True)
        clean_pairs = [
            (name, value)
            for name, values in params.items()
            if name != "token"
            for value in values
        ]
        location = urlunparse(("", "", parsed.path or "/", "", urlencode(clean_pairs), ""))
        self._send_redirect(location or "/", cookie_token=self.server.auth_token)

    def _send_unauthorized(self) -> None:
        payload = b"missing or invalid dashboard token\n"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("www-authenticate", 'Bearer realm="hindsight-dashboard"')
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(_jsonable(payload), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_snapshot(self, query: str) -> None:
        params = parse_qs(query)
        namespace = _param(params, "namespace", self.server.namespace)
        as_of = _param(params, "as_of", None)
        try:
            if as_of:
                snapshot = self.server.historical_snapshot(namespace=namespace, as_of=as_of)
            else:
                snapshot = self.server.current_snapshot(namespace=namespace)
        except Exception as exc:
            self._send_json({"error": safe_error_detail(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(snapshot)

    def _send_events(self, query: str) -> None:
        params = parse_qs(query)
        namespace = _param(params, "namespace", self.server.namespace)
        broker = self.server.broker_for(namespace)
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.send_header("x-accel-buffering", "no")
        self.end_headers()

        subscription: DashboardSubscription | None = None
        try:
            subscription = broker.subscribe()
            self._write_sse("snapshot", subscription.snapshot)
            while True:
                event = subscription.events.get()
                if event is None:
                    break
                self._write_sse(event["event"], event)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._write_sse("error", {"error": safe_error_detail(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            if subscription is not None:
                self.server.release_subscription(namespace=namespace, subscription=subscription)

    def _write_sse(self, event: str, payload: dict[str, Any]) -> None:
        body = f"event: {event}\ndata: {json.dumps(_jsonable(payload), sort_keys=True)}\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    def _authorized(self, params: dict[str, list[str]]) -> bool:
        expected = self.server.auth_token
        if expected is None:
            return True
        supplied = (
            _bearer_token(self.headers.get("authorization"))
            or _cookie_token(self.headers.get("cookie"))
            or _param(params, "token", None)
        )
        return supplied is not None and hmac.compare_digest(supplied, expected)

    def _query_token_valid(self, params: dict[str, list[str]]) -> bool:
        expected = self.server.auth_token
        supplied = _param(params, "token", None)
        return expected is not None and supplied is not None and hmac.compare_digest(supplied, expected)


def run_dashboard_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    namespace: str = DEMO_NAMESPACE,
    db_url: str | None = None,
    auth_token: str | None = None,
) -> None:
    """Run the dashboard HTTP server until interrupted."""

    server = DashboardServer((host, port), namespace=namespace, db_url=db_url, auth_token=auth_token)
    actual_host, actual_port = server.server_address
    print(f"Hindsight memory dashboard: http://{actual_host}:{actual_port}/?namespace={namespace}")
    if server.auth_token is not None:
        print("Dashboard authentication enabled. Visit once with ?token=<token> to set a cookie.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def memory_snapshot(
    *,
    namespace: str,
    as_of: str | datetime | None = None,
    db_url: str | None = None,
    limit: int = MAX_SNAPSHOT_ROWS,
) -> dict[str, Any]:
    """Return current or historical memory state for the dashboard."""

    if not namespace or not namespace.strip():
        raise ValueError("namespace is required")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    timestamp = _parse_timestamp(as_of) if as_of else None
    if timestamp is not None:
        with MemoryStore(url=db_url or database_url()) as store:
            memories = store.list_semantic_as_of(
                namespace=namespace,
                system_as_of=timestamp,
                valid_at=timestamp,
                limit=limit,
            )
        operations = _memory_operations(namespace=namespace, db_url=db_url, limit=limit, as_of=timestamp)
        return {
            "type": "snapshot",
            "mode": "as_of",
            "namespace": namespace,
            "as_of": timestamp.isoformat(),
            "memories": [_normalize_memory(row) for row in memories],
            "operations": [_normalize_operation(row) for row in operations],
            "timeline": _timeline(memories, operations, cutoff=timestamp),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    memories = _semantic_memories(namespace=namespace, db_url=db_url, limit=limit)
    operations = _memory_operations(namespace=namespace, db_url=db_url, limit=limit)
    return {
        "type": "snapshot",
        "mode": "current",
        "namespace": namespace,
        "as_of": None,
        "memories": [_normalize_memory(row) for row in memories],
        "operations": [_normalize_operation(row) for row in operations],
        "timeline": _timeline(memories, operations),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def changefeed_events(
    *,
    namespace: str,
    db_url: str | None = None,
    stop_event: threading.Event | None = None,
    cursor: datetime | None = None,
    on_ready: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield namespace-scoped dashboard events from CockroachDB changefeeds."""

    stop_event = stop_event or threading.Event()
    cursor = cursor or changefeed_cursor(db_url=db_url)
    with connect(db_url or database_url()) as conn:
        conn.autocommit = True
        with conn.cursor(row_factory=dict_row) as cur:
            query = """
                    CREATE CHANGEFEED FOR semantic_memories, memory_operations
                    WITH updated, resolved = '2s', cursor = %s
                """
            if on_ready is not None:
                on_ready()
            for row in cur.stream(query, (cursor.isoformat(),)):
                if stop_event.is_set():
                    break
                event = changefeed_row_to_event(row, namespace=namespace)
                if event is not None:
                    yield event


def changefeed_cursor(*, db_url: str | None = None) -> datetime:
    """Return a CockroachDB-derived timestamp for changefeed cursor startup."""

    with connect(db_url or database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("failed to fetch changefeed cursor timestamp")
            timestamp = row[0]
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


def changefeed_row_to_event(row: dict[str, Any], *, namespace: str) -> dict[str, Any] | None:
    """Normalize one CockroachDB changefeed row into a dashboard SSE event."""

    raw = dict(row)
    if raw.get("resolved") is not None:
        return {
            "event": "resolved",
            "type": "resolved",
            "namespace": namespace,
            "resolved": _jsonable(raw["resolved"]),
        }
    table = raw.get("table")
    value = _decode_json_value(raw.get("value"))
    if not isinstance(value, dict):
        return None
    updated = raw.get("updated")
    if value.get("updated") is not None:
        updated = value["updated"]
    if value.get("resolved") is not None:
        return {
            "event": "resolved",
            "type": "resolved",
            "namespace": namespace,
            "resolved": _jsonable(value["resolved"]),
        }
    if isinstance(value.get("after"), dict):
        value = value["after"]
    if value.get("namespace") != namespace:
        return None
    if table == "semantic_memories":
        return {
            "event": "memory",
            "type": "memory",
            "namespace": namespace,
            "updated": _jsonable(updated),
            "memory": _normalize_memory(value),
        }
    if table == "memory_operations":
        return {
            "event": "operation",
            "type": "operation",
            "namespace": namespace,
            "updated": _jsonable(updated),
            "operation": _normalize_operation(value),
        }
    return None


def _semantic_memories(
    *, namespace: str, db_url: str | None, limit: int
) -> list[dict[str, Any]]:
    with connect(db_url or database_url()) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT *
                    FROM (
                        SELECT *
                        FROM semantic_memories
                        WHERE namespace = %s
                        ORDER BY
                            COALESCE(invalidated_at, written_at, t_invalid, t_valid) DESC,
                            written_at DESC,
                            id DESC
                        LIMIT %s
                    ) AS recent_memories
                    ORDER BY t_valid ASC, written_at ASC, id ASC
                """,
                (namespace, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def _memory_operations(
    *,
    namespace: str,
    db_url: str | None,
    limit: int,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    with connect(db_url or database_url()) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            as_of_filter = "AND created_at <= %s" if as_of is not None else ""
            params: tuple[Any, ...]
            params = (namespace, as_of, limit) if as_of is not None else (namespace, limit)
            cur.execute(
                f"""
                    SELECT *
                    FROM (
                        SELECT *
                        FROM memory_operations
                        WHERE namespace = %s
                            {as_of_filter}
                        ORDER BY created_at DESC, id DESC
                        LIMIT %s
                    ) AS recent_operations
                    ORDER BY created_at ASC, id ASC
                """,
                params,
            )
            operations = [dict(row) for row in cur.fetchall()]
            for operation in operations:
                effect_as_of_filter = "AND created_at <= %s" if as_of is not None else ""
                effect_params = (
                    (operation["id"], as_of) if as_of is not None else (operation["id"],)
                )
                cur.execute(
                    f"""
                        SELECT * FROM memory_operation_effects
                        WHERE operation_id = %s {effect_as_of_filter}
                        ORDER BY sequence
                    """,
                    effect_params,
                )
                operation["effects"] = [dict(row) for row in cur.fetchall()]
            return operations


def _normalize_memory(row: dict[str, Any]) -> dict[str, Any]:
    invalidated = row.get(
        "snapshot_invalidated",
        row.get("t_invalid") is not None or row.get("invalidated_at") is not None,
    )
    return {
        "id": str(row.get("id")),
        "namespace": row.get("namespace"),
        "content": row.get("content"),
        "writer": row.get("writer"),
        "source_ref": row.get("source_ref"),
        "justification": row.get("justification"),
        "metadata": row.get("metadata") or {},
        "belief_id": str(row.get("belief_id")) if row.get("belief_id") else None,
        "version_number": row.get("version_number"),
        "content_schema": row.get("content_schema"),
        "lineage_status": row.get("lineage_status"),
        "trust_status": row.get("trust_status"),
        "t_valid": _jsonable(row.get("t_valid")),
        "t_invalid": _jsonable(row.get("t_invalid")),
        "written_at": _jsonable(row.get("written_at")),
        "invalidated_at": _jsonable(row.get("invalidated_at")),
        "invalidated_by": row.get("invalidated_by"),
        "invalidation_reason": row.get("invalidation_reason"),
        "status": "invalidated" if invalidated else "current",
    }


def _normalize_operation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "operation_type": row.get("operation_type"),
        "actor": row.get("actor"),
        "reason": row.get("reason"),
        "namespace": row.get("namespace"),
        "target_timestamp": _jsonable(row.get("target_timestamp")),
        "invalidated_memory_ids": row.get("invalidated_memory_ids") or [],
        "restored_memory_ids": row.get("restored_memory_ids") or [],
        "status": row.get("status"),
        "failure_code": row.get("failure_code"),
        "failure_detail": row.get("failure_detail"),
        "effects": [_jsonable(item) for item in row.get("effects") or []],
        "created_at": _jsonable(row.get("created_at")),
    }


def _timeline(
    memories: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    *,
    cutoff: datetime | None = None,
) -> list[str]:
    values = set()
    for row in memories:
        for key in ("t_valid", "written_at", "t_invalid", "invalidated_at"):
            if row.get(key) is not None:
                value = row[key]
                if cutoff is None or not isinstance(value, datetime) or value <= cutoff:
                    values.add(_jsonable(value))
    for row in operations:
        for key in ("target_timestamp", "created_at"):
            if row.get(key) is not None:
                value = row[key]
                if cutoff is None or not isinstance(value, datetime) or value <= cutoff:
                    values.add(_jsonable(value))
    return sorted(str(value) for value in values if value is not None)


def _upsert_recent(
    rows: list[dict[str, Any]],
    row: dict[str, Any],
    *,
    sort_key: Callable[[dict[str, Any]], tuple[str, str]],
    limit: int,
) -> list[dict[str, Any]]:
    by_id = {str(item.get("id")): item for item in rows}
    by_id[str(row.get("id"))] = row
    sorted_rows = sorted(by_id.values(), key=sort_key)
    return sorted_rows[-limit:]


def _memory_event_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    timestamp = (
        row.get("invalidated_at")
        or row.get("written_at")
        or row.get("t_invalid")
        or row.get("t_valid")
        or ""
    )
    return (str(timestamp), str(row.get("id") or ""))


def _operation_event_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("created_at") or ""), str(row.get("id") or ""))


def _put_sse_event(
    subscriber: "queue.Queue[dict[str, Any] | None]",
    event: dict[str, Any] | None,
) -> bool:
    try:
        subscriber.put_nowait(event)
        return True
    except queue.Full:
        return False


def _parse_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _param(params: dict[str, list[str]], name: str, default: str | None) -> str | None:
    values = params.get(name)
    if not values:
        return default
    value = values[0].strip()
    return value or default


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _cookie_token(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        name, _, raw = part.strip().partition("=")
        if name == DASHBOARD_AUTH_COOKIE and raw:
            return unquote(raw)
    return None


def _optional_token(value: str | None) -> str | None:
    if value is None:
        return None
    token = value.strip()
    return token or None


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _legacy_dashboard_html(*, default_namespace: str = DEMO_NAMESPACE) -> str:
    """Return the dashboard page."""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hindsight Memory Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101214;
      --panel: #171b1f;
      --panel-2: #20262b;
      --line: #3b444d;
      --text: #f2f5f3;
      --muted: #a8b3ad;
      --accent: #6ee7b7;
      --bad: #fb7185;
      --warn: #fbbf24;
      --good: #86efac;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100dvh;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      min-height: 100dvh;
      padding: 24px;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 18px;
    }}
    header {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 20px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(34px, 4.2vw, 64px);
      line-height: 0.95;
      letter-spacing: 0;
    }}
    .namespace {{
      color: var(--accent);
      font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
      font-size: 15px;
      overflow-wrap: anywhere;
      margin-top: 8px;
    }}
    .status {{
      min-width: 220px;
      text-align: right;
      color: var(--muted);
      font-size: 18px;
    }}
    .status strong {{ color: var(--accent); font-weight: 700; }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(240px, 1fr) auto;
      gap: 16px;
      align-items: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    input[type="range"] {{ width: 100%; accent-color: var(--accent); }}
    button {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
      color: var(--text);
      font-size: 16px;
      padding: 10px 14px;
      cursor: pointer;
    }}
    button:active {{ transform: translateY(1px); }}
    .time-label {{
      color: var(--muted);
      font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
      font-size: 14px;
      margin-top: 8px;
      min-height: 18px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(320px, 0.85fr);
      gap: 18px;
      min-height: 0;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 0;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    .section-title {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      font-size: 22px;
      font-weight: 700;
    }}
    .count {{ color: var(--muted); font-size: 16px; font-weight: 500; }}
    .list {{
      overflow: auto;
      padding: 14px;
      display: grid;
      gap: 12px;
      align-content: start;
    }}
    .memory {{
      border: 1px solid var(--line);
      border-left: 6px solid var(--accent);
      border-radius: 8px;
      background: var(--panel-2);
      padding: 14px;
      display: grid;
      gap: 10px;
    }}
    .memory.invalidated {{
      border-left-color: var(--bad);
      opacity: 0.72;
    }}
    .memory.invalidated .content {{ text-decoration: line-through; }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 14px;
      font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
    }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      background: #111518;
    }}
    .pill.bad {{ color: var(--bad); }}
    .pill.good {{ color: var(--good); }}
    .content {{
      font-size: clamp(17px, 1.45vw, 24px);
      line-height: 1.35;
    }}
    .reason {{
      color: var(--bad);
      font-size: 15px;
    }}
    .operation {{
      border: 1px solid var(--line);
      border-left: 6px solid var(--warn);
      border-radius: 8px;
      background: var(--panel-2);
      padding: 14px;
      display: grid;
      gap: 8px;
      font-size: 16px;
    }}
    .empty {{
      color: var(--muted);
      padding: 18px;
      font-size: 20px;
    }}
    @media (max-width: 900px) {{
      main {{ padding: 14px; }}
      header {{ align-items: start; flex-direction: column; }}
      .status {{ text-align: left; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Memory Dashboard</h1>
        <div class="namespace" id="namespace"></div>
      </div>
      <div class="status" id="status">Connecting</div>
    </header>
    <div class="toolbar">
      <div>
        <input id="timeline" type="range" min="0" max="0" value="0" disabled>
        <div class="time-label" id="timeLabel">Live belief state</div>
      </div>
      <button id="liveButton" type="button">Live</button>
    </div>
    <div class="grid">
      <section>
        <div class="section-title">
          <span id="beliefTitle">Current Beliefs</span>
          <span class="count" id="memoryCount">0</span>
        </div>
        <div class="list" id="memories"></div>
      </section>
      <section>
        <div class="section-title">
          <span>Rewinds</span>
          <span class="count" id="operationCount">0</span>
        </div>
        <div class="list" id="operations"></div>
      </section>
    </div>
  </main>
  <script>
    const params = new URLSearchParams(window.location.search);
    const namespace = params.get("namespace") || {json.dumps(default_namespace)};
    const state = {{ memories: new Map(), operations: new Map(), timeline: [], asOf: null }};
    let snapshotAbortController = null;
    let snapshotRequestSeq = 0;
    let timelineDebounceId = null;
    const namespaceEl = document.getElementById("namespace");
    const statusEl = document.getElementById("status");
    const memoriesEl = document.getElementById("memories");
    const operationsEl = document.getElementById("operations");
    const timelineEl = document.getElementById("timeline");
    const timeLabelEl = document.getElementById("timeLabel");
    const liveButton = document.getElementById("liveButton");
    namespaceEl.textContent = namespace;

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({{
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }}[char]));
    }}

    function shortId(id) {{ return String(id || "").slice(0, 8); }}

    function applySnapshot(snapshot) {{
      state.memories = new Map((snapshot.memories || []).map((memory) => [memory.id, memory]));
      state.operations = new Map((snapshot.operations || []).map((operation) => [operation.id, operation]));
      state.timeline = snapshot.timeline || [];
      state.asOf = snapshot.as_of || null;
      render();
    }}

    function mergeTimeline(values) {{
      const merged = new Set(state.timeline);
      values.filter(Boolean).forEach((value) => merged.add(value));
      state.timeline = Array.from(merged).sort();
    }}

    function render() {{
      const memories = Array.from(state.memories.values());
      const current = memories.filter((memory) => memory.status !== "invalidated");
      const invalidated = memories.filter((memory) => memory.status === "invalidated");
      document.getElementById("beliefTitle").textContent = state.asOf ? "Beliefs As Of" : "Current Beliefs";
      document.getElementById("memoryCount").textContent = `${{current.length}} live / ${{invalidated.length}} invalid`;
      document.getElementById("operationCount").textContent = `${{state.operations.size}}`;
      memoriesEl.innerHTML = memories.length ? memories.map(renderMemory).join("") : '<div class="empty">Waiting for memory events.</div>';
      operationsEl.innerHTML = state.operations.size
        ? Array.from(state.operations.values()).map(renderOperation).join("")
        : '<div class="empty">No rewind operations yet.</div>';
      timelineEl.max = Math.max(state.timeline.length - 1, 0);
      timelineEl.disabled = state.timeline.length === 0;
      if (!state.asOf) {{
        timelineEl.value = state.timeline.length ? state.timeline.length - 1 : 0;
        timeLabelEl.textContent = "Live belief state";
      }} else {{
        timelineEl.value = Math.max(state.timeline.indexOf(state.asOf), 0);
        timeLabelEl.textContent = `Belief state as of ${{state.asOf}}`;
      }}
    }}

    function renderMemory(memory) {{
      const invalid = memory.status === "invalidated";
      const reason = memory.invalidation_reason || "";
      return `<article class="memory ${{invalid ? "invalidated" : ""}}" title="${{escapeHtml(reason)}}">
        <div class="meta">
          <span class="pill ${{invalid ? "bad" : "good"}}">${{invalid ? "invalidated" : "current"}}</span>
          <span class="pill">${{escapeHtml(memory.writer)}}</span>
          <span class="pill">${{shortId(memory.id)}}</span>
        </div>
        <div class="content">${{escapeHtml(memory.content)}}</div>
        <div class="meta">
          <span>${{escapeHtml(memory.source_ref)}}</span>
          <span>${{escapeHtml(memory.written_at || memory.t_valid)}}</span>
        </div>
        ${{reason ? `<div class="reason">${{escapeHtml(reason)}}</div>` : ""}}
      </article>`;
    }}

    function renderOperation(operation) {{
      return `<article class="operation">
        <div class="meta">
          <span class="pill">${{escapeHtml(operation.operation_type)}}</span>
          <span class="pill">${{shortId(operation.id)}}</span>
        </div>
        <div>${{escapeHtml(operation.reason)}}</div>
        <div class="meta">
          <span>invalidated ${{(operation.invalidated_memory_ids || []).length}}</span>
          <span>${{escapeHtml(operation.created_at)}}</span>
        </div>
      </article>`;
    }}

    function reportSnapshotError(error) {{
      if (error.name === "AbortError") return;
      statusEl.textContent = "Snapshot failed";
      console.error(error);
    }}

    async function loadSnapshot(asOf = null) {{
      const requestSeq = ++snapshotRequestSeq;
      if (snapshotAbortController) snapshotAbortController.abort();
      snapshotAbortController = new AbortController();
      const controller = snapshotAbortController;
      const url = new URL("/snapshot", window.location.origin);
      url.searchParams.set("namespace", namespace);
      if (asOf) url.searchParams.set("as_of", asOf);
      const response = await fetch(url, {{ signal: controller.signal }});
      if (!response.ok) throw new Error(await response.text());
      const snapshot = await response.json();
      if (requestSeq === snapshotRequestSeq) applySnapshot(snapshot);
      if (snapshotAbortController === controller) snapshotAbortController = null;
    }}

    function scheduleTimelineSnapshot(asOf) {{
      clearTimeout(timelineDebounceId);
      timelineDebounceId = setTimeout(() => {{
        loadSnapshot(asOf).catch(reportSnapshotError);
      }}, 150);
    }}

    timelineEl.addEventListener("input", () => {{
      const selected = state.timeline[Number(timelineEl.value)];
      if (!selected) return;
      scheduleTimelineSnapshot(selected);
    }});

    liveButton.addEventListener("click", async () => {{
      clearTimeout(timelineDebounceId);
      state.asOf = null;
      await loadSnapshot().catch(reportSnapshotError);
    }});

    const events = new EventSource(`/events?namespace=${{encodeURIComponent(namespace)}}`);
    events.addEventListener("open", () => {{ statusEl.innerHTML = "<strong>Live</strong>"; }});
    events.addEventListener("snapshot", (event) => applySnapshot(JSON.parse(event.data)));
    events.addEventListener("memory", (event) => {{
      const payload = JSON.parse(event.data);
      state.memories.set(payload.memory.id, payload.memory);
      mergeTimeline([payload.memory.t_valid, payload.memory.written_at, payload.memory.t_invalid, payload.memory.invalidated_at]);
      if (!state.asOf) render();
    }});
    events.addEventListener("operation", (event) => {{
      const payload = JSON.parse(event.data);
      state.operations.set(payload.operation.id, payload.operation);
      mergeTimeline([payload.operation.target_timestamp, payload.operation.created_at]);
      if (!state.asOf) render();
    }});
    events.addEventListener("error", () => {{ statusEl.textContent = "Reconnecting"; }});
  </script>
</body>
</html>"""


def dashboard_html(*, default_namespace: str = DEMO_NAMESPACE) -> str:
    """Return the enhanced static cockpit with local SSE configuration inlined."""

    from importlib.resources import files

    try:
        web_root = files("hindsight").joinpath("web")
        html = web_root.joinpath("index.html").read_text(encoding="utf-8")
        styles = web_root.joinpath("styles.css").read_text(encoding="utf-8")
        script = web_root.joinpath("app.js").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return _legacy_dashboard_html(default_namespace=default_namespace)
    local_config = {
        "apiBase": "/v1",
        "snapshotBase": "/snapshot",
        "eventsBase": "/events",
        "websocketUrl": None,
        "defaultNamespace": default_namespace,
        "pollIntervalMs": 4000,
    }
    html = html.replace('<link rel="stylesheet" href="/styles.css">', f"<style>{styles}</style>")
    html = html.replace(
        '<script src="/config.js"></script>',
        f"<script>window.HINDSIGHT_CONFIG = {json.dumps(local_config)};</script>",
    )
    html = html.replace(
        '<script type="module" src="/app.js"></script>',
        f'<script type="module">{script}</script>',
    )
    return html
