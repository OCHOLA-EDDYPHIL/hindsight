"""Shared Gemini credential routing with bounded, observable failover."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, Protocol, TypeVar

from hindsight.aws import aws_client_config

T = TypeVar("T")


class GeminiPoolError(RuntimeError):
    """Base error for Gemini credential-pool failures."""


class GeminiPoolExhaustedError(GeminiPoolError):
    """Raised after every eligible credential has failed or is cooling down."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class GeminiCredential:
    slot_id: str
    api_key: str


@dataclass(frozen=True)
class CooldownState:
    cooldown_until: int
    failure_count: int = 0


@dataclass(frozen=True)
class PoolExecution(Generic[T]):
    value: T
    slot_id: str
    attempts: int


@dataclass(frozen=True)
class FailureDisposition:
    retryable: bool
    category: str
    cooldown_seconds: int = 0


class CooldownStore(Protocol):
    def get_states(self, slot_ids: list[str]) -> dict[str, CooldownState]: ...

    def record_failure(
        self,
        slot_id: str,
        *,
        cooldown_until: int,
        error_code: str,
        now: int,
    ) -> None: ...

    def record_success(self, slot_id: str, *, operation_started_at: int) -> None: ...


class InMemoryCooldownStore:
    """Process-local fallback used by local runs and tests."""

    def __init__(self):
        self._states: dict[str, CooldownState] = {}

    def get_states(self, slot_ids: list[str]) -> dict[str, CooldownState]:
        return {slot: self._states[slot] for slot in slot_ids if slot in self._states}

    def record_failure(
        self,
        slot_id: str,
        *,
        cooldown_until: int,
        error_code: str,
        now: int,
    ) -> None:
        previous = self._states.get(slot_id)
        self._states[slot_id] = CooldownState(
            cooldown_until=max(cooldown_until, previous.cooldown_until if previous else 0),
            failure_count=(previous.failure_count if previous else 0) + 1,
        )

    def record_success(self, slot_id: str, *, operation_started_at: int) -> None:
        state = self._states.get(slot_id)
        if state is None or state.cooldown_until <= operation_started_at:
            self._states.pop(slot_id, None)


class DynamoDbCooldownStore:
    """Cross-container cooldown state backed by one on-demand DynamoDB table."""

    def __init__(self, *, table_name: str, client: Any):
        self._table_name = table_name
        self._client = client

    def get_states(self, slot_ids: list[str]) -> dict[str, CooldownState]:
        response = self._client.batch_get_item(
            RequestItems={
                self._table_name: {
                    "Keys": [{"slot_id": {"S": slot_id}} for slot_id in slot_ids],
                    "ConsistentRead": True,
                }
            }
        )
        states: dict[str, CooldownState] = {}
        for item in response.get("Responses", {}).get(self._table_name, []):
            slot_id = item["slot_id"]["S"]
            states[slot_id] = CooldownState(
                cooldown_until=int(item.get("cooldown_until", {"N": "0"})["N"]),
                failure_count=int(item.get("failure_count", {"N": "0"})["N"]),
            )
        return states

    def record_failure(
        self,
        slot_id: str,
        *,
        cooldown_until: int,
        error_code: str,
        now: int,
    ) -> None:
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"slot_id": {"S": slot_id}},
                UpdateExpression=(
                    "SET cooldown_until = :until, expires_at = :expires, "
                    "last_error_code = :code, updated_at = :now "
                    "ADD failure_count :one"
                ),
                ConditionExpression=(
                    "attribute_not_exists(cooldown_until) OR cooldown_until < :until"
                ),
                ExpressionAttributeValues={
                    ":until": {"N": str(cooldown_until)},
                    ":expires": {"N": str(cooldown_until + 86_400)},
                    ":code": {"S": error_code},
                    ":now": {"N": str(now)},
                    ":one": {"N": "1"},
                },
            )
        except Exception as exc:
            if _aws_error_code(exc) != "ConditionalCheckFailedException":
                raise

    def record_success(self, slot_id: str, *, operation_started_at: int) -> None:
        try:
            self._client.delete_item(
                TableName=self._table_name,
                Key={"slot_id": {"S": slot_id}},
                ConditionExpression=(
                    "attribute_not_exists(cooldown_until) OR cooldown_until <= :started"
                ),
                ExpressionAttributeValues={
                    ":started": {"N": str(operation_started_at)},
                },
            )
        except Exception as exc:
            if _aws_error_code(exc) != "ConditionalCheckFailedException":
                raise


class FailOpenCooldownStore:
    """Preserve model availability when the shared cooldown registry is unavailable."""

    def __init__(self, primary: CooldownStore, fallback: CooldownStore | None = None):
        self._primary = primary
        self._fallback = fallback or InMemoryCooldownStore()

    def get_states(self, slot_ids: list[str]) -> dict[str, CooldownState]:
        local = self._fallback.get_states(slot_ids)
        try:
            shared = self._primary.get_states(slot_ids)
        except Exception:
            return local
        for slot_id, state in local.items():
            current = shared.get(slot_id)
            if current is None or state.cooldown_until > current.cooldown_until:
                shared[slot_id] = state
        return shared

    def record_failure(
        self,
        slot_id: str,
        *,
        cooldown_until: int,
        error_code: str,
        now: int,
    ) -> None:
        self._fallback.record_failure(
            slot_id,
            cooldown_until=cooldown_until,
            error_code=error_code,
            now=now,
        )
        try:
            self._primary.record_failure(
                slot_id,
                cooldown_until=cooldown_until,
                error_code=error_code,
                now=now,
            )
        except Exception:
            pass

    def record_success(self, slot_id: str, *, operation_started_at: int) -> None:
        self._fallback.record_success(slot_id, operation_started_at=operation_started_at)
        try:
            self._primary.record_success(slot_id, operation_started_at=operation_started_at)
        except Exception:
            pass


class GeminiCredentialPool:
    """Route one Gemini operation across independently quota-limited projects."""

    def __init__(
        self,
        credentials: list[GeminiCredential],
        *,
        cooldown_store: CooldownStore | None = None,
        client_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float, float], float] = random.uniform,
    ):
        if not credentials:
            raise GeminiPoolError("At least one Gemini API key is required")
        slot_ids = [credential.slot_id for credential in credentials]
        if len(slot_ids) != len(set(slot_ids)):
            raise GeminiPoolError("Gemini key slot IDs must be unique")
        if len({credential.api_key for credential in credentials}) != len(credentials):
            raise GeminiPoolError("Gemini API keys must be unique")
        self._credentials = tuple(credentials)
        self._cooldown_store = cooldown_store or InMemoryCooldownStore()
        self._client_factory = client_factory or _gemini_client
        self._clock = clock
        self._jitter = jitter
        self._clients: dict[str, Any] = {}

    @property
    def slot_ids(self) -> tuple[str, ...]:
        return tuple(credential.slot_id for credential in self._credentials)

    def execute(
        self,
        operation: Callable[[Any], T],
        *,
        routing_key: str,
    ) -> PoolExecution[T]:
        started_at = int(self._clock())
        states = self._cooldown_store.get_states(list(self.slot_ids))
        ordered = self._ordered_credentials(routing_key)
        eligible = [
            credential
            for credential in ordered
            if states.get(credential.slot_id, CooldownState(0)).cooldown_until <= started_at
        ]
        if not eligible:
            retry_at = min(state.cooldown_until for state in states.values())
            raise GeminiPoolExhaustedError(
                "All Gemini key slots are cooling down",
                retry_after_seconds=max(1, retry_at - started_at),
            )

        attempts = 0
        retry_after: int | None = None
        failures: list[str] = []
        for credential in eligible:
            attempts += 1
            try:
                value = operation(self._client_for(credential))
            except Exception as exc:
                state = states.get(credential.slot_id, CooldownState(0))
                disposition = classify_gemini_failure(
                    exc,
                    failure_count=state.failure_count + 1,
                    jitter=self._jitter,
                )
                if not disposition.retryable:
                    raise
                cooldown_until = started_at + disposition.cooldown_seconds
                self._cooldown_store.record_failure(
                    credential.slot_id,
                    cooldown_until=cooldown_until,
                    error_code=disposition.category,
                    now=started_at,
                )
                retry_after = (
                    disposition.cooldown_seconds
                    if retry_after is None
                    else min(retry_after, disposition.cooldown_seconds)
                )
                failures.append(disposition.category)
                continue
            self._cooldown_store.record_success(
                credential.slot_id,
                operation_started_at=started_at,
            )
            return PoolExecution(value=value, slot_id=credential.slot_id, attempts=attempts)

        categories = ",".join(sorted(set(failures))) or "unavailable"
        raise GeminiPoolExhaustedError(
            f"Every eligible Gemini key slot failed ({categories})",
            retry_after_seconds=retry_after,
        )

    def _ordered_credentials(self, routing_key: str) -> list[GeminiCredential]:
        digest = hashlib.sha256(routing_key.encode("utf-8")).digest()
        start = int.from_bytes(digest[:8], "big") % len(self._credentials)
        return [
            self._credentials[(start + offset) % len(self._credentials)]
            for offset in range(len(self._credentials))
        ]

    def _client_for(self, credential: GeminiCredential) -> Any:
        client = self._clients.get(credential.slot_id)
        if client is None:
            client = self._client_factory(credential.api_key)
            self._clients[credential.slot_id] = client
        return client


def gemini_pool_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    cooldown_store: CooldownStore | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> GeminiCredentialPool:
    env = os.environ if environ is None else environ
    credentials = parse_gemini_credentials(env)
    store = cooldown_store
    table_name = (env.get("HINDSIGHT_GEMINI_KEY_HEALTH_TABLE") or "").strip()
    if store is None and table_name:
        import boto3

        client = boto3.client(
            "dynamodb",
            region_name=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION"),
            config=aws_client_config(read_timeout=10),
        )
        store = FailOpenCooldownStore(
            DynamoDbCooldownStore(table_name=table_name, client=client)
        )
    return GeminiCredentialPool(
        credentials,
        cooldown_store=store,
        client_factory=client_factory,
    )


def parse_gemini_credentials(environ: Mapping[str, str]) -> list[GeminiCredential]:
    document = (environ.get("GEMINI_API_KEYS") or "").strip()
    if document:
        return _credentials_from_document(document)
    values = []
    for name in ("GEMINI_API_KEY", *(f"GEMINI_API_KEY_{index}" for index in range(1, 20))):
        value = (environ.get(name) or "").strip()
        if value:
            values.append(value)
    if not values:
        raise GeminiPoolError("GEMINI_API_KEY or GEMINI_API_KEYS is required for Gemini")
    return [
        GeminiCredential(slot_id=f"gemini-{index + 1}", api_key=value)
        for index, value in enumerate(values)
    ]


def classify_gemini_failure(
    exc: Exception,
    *,
    failure_count: int,
    jitter: Callable[[float, float], float] = random.uniform,
) -> FailureDisposition:
    status = _status_code(exc)
    retry_after = _retry_after_seconds(exc)
    if status == 429:
        base = retry_after or min(900, 30 * (2 ** max(0, failure_count - 1)))
        return FailureDisposition(True, "rate_limit", _with_jitter(base, jitter))
    if status in {401, 403}:
        return FailureDisposition(True, "authentication", 3_600)
    if status is not None and 500 <= status <= 599:
        base = min(120, 5 * (2 ** max(0, failure_count - 1)))
        return FailureDisposition(True, "provider_transient", _with_jitter(base, jitter))
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        base = min(120, 5 * (2 ** max(0, failure_count - 1)))
        return FailureDisposition(True, "network", _with_jitter(base, jitter))
    return FailureDisposition(False, "request")


def _credentials_from_document(document: str) -> list[GeminiCredential]:
    try:
        payload = json.loads(document)
    except json.JSONDecodeError:
        return [GeminiCredential(slot_id="gemini-1", api_key=document)]
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and payload.get("version") == 1:
        entries = payload.get("keys")
    else:
        raise GeminiPoolError("GEMINI_API_KEYS must be a version 1 key document")
    if not isinstance(entries, list) or not entries:
        raise GeminiPoolError("GEMINI_API_KEYS must contain at least one key")
    credentials = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            slot_id = f"gemini-{index + 1}"
            api_key = entry
        elif isinstance(entry, dict):
            slot_id = str(entry.get("id") or f"gemini-{index + 1}").strip()
            api_key = str(entry.get("api_key") or "").strip()
        else:
            raise GeminiPoolError("Each Gemini key entry must be a string or object")
        if not slot_id or not api_key:
            raise GeminiPoolError("Each Gemini key entry requires an ID and API key")
        credentials.append(GeminiCredential(slot_id=slot_id, api_key=api_key))
    return credentials


def _gemini_client(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


def _status_code(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _retry_after_seconds(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(1, int(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _with_jitter(base: int, jitter: Callable[[float, float], float]) -> int:
    return max(1, int(round(jitter(base * 0.8, base * 1.2))))


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None
