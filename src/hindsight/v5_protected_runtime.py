"""Durable single-attempt control and audit surfaces for V5 protected execution."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import ClientError

from hindsight.evidence_archive import canonical_json_bytes
from hindsight.memory import MemoryGovernance, MemoryStore, Provenance
from hindsight.reasoning import ReasoningProvider, ReasoningRequest
from hindsight.v5_corpus import ACTION_BUDGET, V5IncidentSimulator, applicability_matches, sha256_hex
from hindsight.v5_protected import (
    ACTION_RESPONSE_SCHEMA,
    ACTION_SYSTEM_PROMPT,
    PROTECTED_ARMS,
    PROTECTED_REPETITIONS,
    deterministic_arm_order,
    reference_lesson,
    write_private_json_exclusive,
)
from hindsight.v5_governance import CacheOnlyEmbeddingProvider
from hindsight.v5_qualification import (
    CheckpointedEmbeddingProvider,
    QualificationStore,
    _candidate_is_positive,
    _candidate_memories,
    _remember_candidate_with_retry,
    _result_hits,
    _result_value,
    _seal_retrieval_decision,
    _verify_retrieval_trace,
    candidate_database_metadata,
    candidate_database_payload,
    render_retrieval_document,
    render_retrieval_query,
)


RunKind = Literal["development_pilot", "protected"]
AuditCategory = Literal[
    "authorization",
    "retrieval_decision",
    "memory_read",
    "reasoning_response",
    "outcome",
    "rollback",
    "monitoring",
]
ROLLBACK_CLASS = {
    "safety_failure": "hard_gate_failure",
    "hard_gate_failure": "hard_gate_failure",
    "tenant_isolation_breach": "hard_gate_failure",
    "embedding_cache_miss": "integrity_failure",
    "invalid_signature": "integrity_failure",
    "artifact_integrity_failure": "integrity_failure",
    "audit_integrity_failure": "integrity_failure",
    "monitoring_outage": "monitoring_unavailable",
    "audit_sink_unavailable": "monitoring_unavailable",
}
_ACTION_RE = re.compile(r"[a-z_]+")


class ArmContextProvider(Protocol):
    def context(
        self,
        *,
        scenario: Mapping[str, Any],
        arm: str,
        trial_id: str,
        step: int,
    ) -> Mapping[str, Any]: ...


class ProtectedRunFailure(RuntimeError):
    """A frozen protected control failed and must terminate the attempt."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _behavioral_retrieval_hard_gate(
    *,
    policy: Any,
    fallback_reason: Any,
    target_rank_one: bool,
    target_absent: bool,
    known_membership: bool,
) -> bool:
    """Keep semantic rank as efficacy evidence, not a per-trial safety gate."""

    return (
        policy == "semantic_strict"
        and fallback_reason is None
        and target_absent
        and known_membership
    )


class ProtectedAuditArchive(Protocol):
    def put_json(self, *, key: str, payload: Any) -> Mapping[str, Any]: ...

    def put_audit_event(
        self, *, run_id: str, event: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class ProtectedAuditStore:
    """Own one filesystem-fenced attempt and its write-once archived event chain."""

    def __init__(
        self,
        *,
        directory: str | os.PathLike[str],
        archive: ProtectedAuditArchive,
    ) -> None:
        self._directory = pathlib.Path(directory).expanduser().resolve(strict=False)
        self._archive = archive
        self._lock = threading.RLock()

    def claim(
        self,
        *,
        run_kind: RunKind,
        execution_contract_sha256: str,
        protected_authorization_sha256: str,
        exact_code_sha: str,
        claim_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Consume the sole attempt locally and in the immutable archive."""

        if run_kind not in {"development_pilot", "protected"}:
            raise ValueError("v5 protected run kind is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", execution_contract_sha256):
            raise ValueError("v5 protected execution contract identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", protected_authorization_sha256):
            raise ValueError("v5 protected authorization identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", exact_code_sha):
            raise ValueError("v5 protected exact-code identity is invalid")
        run_id = _run_id(
            run_kind=run_kind,
            authorization_sha256=protected_authorization_sha256,
            contract_sha256=execution_contract_sha256,
        )
        with self._lock:
            claim_path = self._run_directory(run_id) / "claim.json"
            if claim_path.exists():
                raise RuntimeError("v5 protected study attempt is already consumed")
            claim = {
                "schema_version": 1,
                "record_type": "claim",
                "id": run_id,
                "run_kind": run_kind,
                "execution_contract_sha256": execution_contract_sha256,
                "protected_authorization_sha256": protected_authorization_sha256,
                "exact_code_sha": exact_code_sha,
                "claim_payload": dict(claim_payload),
            }
            write_private_json_exclusive(claim_path, claim)
            receipt = self._archive.put_json(
                key=f"learning/v5/protected-studies/{run_id}/claim.json",
                payload=claim,
            )
            if receipt.get("created") is not True:
                raise RuntimeError("v5 protected study attempt is already consumed")
            self._write_archive_receipt(
                run_id=run_id,
                name="claim",
                receipt=receipt,
            )
            self.append(
                run_id=run_id,
                category="authorization",
                payload={
                    "event": "attempt_consumed",
                    "run_kind": run_kind,
                    "execution_contract_sha256": execution_contract_sha256,
                    "protected_authorization_sha256": protected_authorization_sha256,
                    "exact_code_sha": exact_code_sha,
                    "claim_payload_sha256": sha256_hex(dict(claim_payload)),
                },
            )
            self.append(
                run_id=run_id,
                category="rollback",
                payload={"event": "rollback_armed", "state": "armed"},
            )
            return self.get_run(run_id=run_id)

    def start(self, *, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self.get_run(run_id=run_id)
            if run["status"] != "claimed":
                raise RuntimeError("v5 protected study is not claimable for execution")
            record = {
                "schema_version": 1,
                "record_type": "start",
                "run_id": run_id,
                "claim_sha256": sha256_hex(self._load_claim(run_id)),
            }
            self._write_archived_record(run_id=run_id, name="start", value=record)
            return self.get_run(run_id=run_id)

    def append(
        self,
        *,
        run_id: str,
        category: AuditCategory,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            run = self.get_run(run_id=run_id)
            if run["status"] == "terminal":
                raise RuntimeError("v5 protected audit run is terminal")
            events = self.audit_events(run_id=run_id)
            previous = events[-1]["event_sha256"] if events else None
            sequence = len(events) + 1
            body = _event_body(
                run_id=run_id,
                sequence=sequence,
                category=category,
                payload=dict(payload),
                previous_event_sha256=previous,
            )
            event = {**body, "event_sha256": sha256_hex(body)}
            event_path = self._events_directory(run_id) / f"{sequence:08d}.json"
            write_private_json_exclusive(event_path, event)
            receipt = self._archive.put_audit_event(run_id=run_id, event=event)
            self._write_archive_receipt(
                run_id=run_id,
                name=f"event-{sequence:08d}",
                receipt=receipt,
            )
            return event

    def finish(
        self,
        *,
        run_id: str,
        terminal_status: str,
        terminal_payload: Mapping[str, Any],
        rollback_executed: bool,
        rollback_reason: str | None = None,
    ) -> dict[str, Any]:
        """Append rollback state and write one terminal record even if archival fails."""

        rollback_state = "executed" if rollback_executed else "disarmed"
        event = "rollback_executed" if rollback_executed else "rollback_disarmed"
        archive_error: Exception | None = None
        try:
            self.append(
                run_id=run_id,
                category="rollback",
                payload={
                    "event": event,
                    "state": rollback_state,
                    "reason": rollback_reason,
                    "rollback_class": (
                        rollback_class(rollback_reason) if rollback_executed else None
                    ),
                    "action": (
                        "disable-protected-learning-and-consume-authorization"
                        if rollback_executed
                        else "retain-terminal-single-attempt-fence"
                    ),
                },
            )
        except Exception as exc:
            archive_error = exc
            if not rollback_executed:
                rollback_executed = True
                rollback_state = "executed"
                terminal_status = "rolled_back"
                terminal_payload = {
                    "invalidated_terminal_payload": dict(terminal_payload),
                    "failure": "audit_sink_unavailable",
                }
                rollback_reason = "audit_sink_unavailable"
                self._append_local_only(
                    run_id=run_id,
                    category="rollback",
                    payload={
                        "event": "rollback_executed",
                        "state": "executed",
                        "reason": rollback_reason,
                        "rollback_class": rollback_class(rollback_reason),
                        "action": "disable-protected-learning-and-consume-authorization",
                    },
                )
        with self._lock:
            if (self._run_directory(run_id) / "terminal.json").exists():
                raise RuntimeError("v5 protected study is already terminal")
            terminal = {
                "schema_version": 1,
                "record_type": "terminal",
                "run_id": run_id,
                "terminal_status": terminal_status,
                "terminal_payload": dict(terminal_payload),
                "rollback_state": rollback_state,
                "rollback_reason": rollback_reason,
                "audit_head_sha256": self._audit_head(run_id),
                "audit_event_count": len(self.audit_events(run_id=run_id)),
            }
            try:
                self._write_archived_record(
                    run_id=run_id,
                    name="terminal",
                    value=terminal,
                )
            except Exception as exc:
                if archive_error is None:
                    archive_error = exc
                terminal_path = self._run_directory(run_id) / "terminal.json"
                if not terminal_path.exists():
                    write_private_json_exclusive(terminal_path, terminal)
            if archive_error is not None:
                raise archive_error
            return self.get_run(run_id=run_id)

    def get_run(self, *, run_id: str) -> dict[str, Any]:
        claim = self._load_claim(run_id)
        start_path = self._run_directory(run_id) / "start.json"
        terminal_path = self._run_directory(run_id) / "terminal.json"
        terminal = _load_json_file(terminal_path) if terminal_path.exists() else None
        events = self.audit_events(run_id=run_id)
        result = {
            **claim,
            "status": "terminal" if terminal is not None else ("running" if start_path.exists() else "claimed"),
            "rollback_state": (
                terminal["rollback_state"] if terminal is not None else "armed"
            ),
            "audit_event_count": len(events),
            "audit_head_sha256": events[-1]["event_sha256"] if events else None,
        }
        if terminal is not None:
            result.update(
                {
                    "terminal_status": terminal["terminal_status"],
                    "terminal_payload": terminal["terminal_payload"],
                }
            )
        return result

    def audit_events(self, *, run_id: str) -> list[dict[str, Any]]:
        directory = self._events_directory(run_id)
        if not directory.exists():
            return []
        return [_load_json_file(path) for path in sorted(directory.glob("[0-9]" * 8 + ".json"))]

    def verify(
        self,
        *,
        run_id: str,
        required_categories: Sequence[str] = (),
    ) -> dict[str, Any]:
        run = self.get_run(run_id=run_id)
        events = self.audit_events(run_id=run_id)
        previous = None
        categories = set()
        for sequence, event in enumerate(events, start=1):
            body = _event_body(
                run_id=run_id,
                sequence=sequence,
                category=str(event["category"]),
                payload=dict(event["payload"]),
                previous_event_sha256=previous,
            )
            if (
                int(event["sequence"]) != sequence
                or event["previous_event_sha256"] != previous
                or event["event_sha256"] != sha256_hex(body)
            ):
                raise ValueError("v5 protected audit chain differs")
            receipt_path = self._archive_directory(run_id) / f"event-{sequence:08d}.json"
            if not receipt_path.exists():
                raise ValueError("v5 protected audit event is not archived")
            previous = str(event["event_sha256"])
            categories.add(str(event["category"]))
        if run["audit_event_count"] != len(events) or run["audit_head_sha256"] != previous:
            raise ValueError("v5 protected run audit head differs")
        missing = set(required_categories) - categories
        if missing:
            raise ValueError(
                "v5 protected audit categories are incomplete: "
                + ", ".join(sorted(missing))
            )
        return {
            "run": run,
            "event_count": len(events),
            "audit_head_sha256": previous,
            "categories": sorted(categories),
        }

    def _load_claim(self, run_id: str) -> dict[str, Any]:
        path = self._run_directory(run_id) / "claim.json"
        if not path.exists():
            raise ValueError("v5 protected study run is absent")
        claim = _load_json_file(path)
        if claim.get("id") != run_id or claim.get("record_type") != "claim":
            raise ValueError("v5 protected study claim identity differs")
        return claim

    def _append_local_only(
        self,
        *,
        run_id: str,
        category: AuditCategory,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        events = self.audit_events(run_id=run_id)
        previous = events[-1]["event_sha256"] if events else None
        sequence = len(events) + 1
        body = _event_body(
            run_id=run_id,
            sequence=sequence,
            category=category,
            payload=dict(payload),
            previous_event_sha256=previous,
        )
        event = {**body, "event_sha256": sha256_hex(body)}
        write_private_json_exclusive(
            self._events_directory(run_id) / f"{sequence:08d}.json",
            event,
        )
        return event

    def _write_archived_record(
        self, *, run_id: str, name: str, value: Mapping[str, Any]
    ) -> None:
        path = self._run_directory(run_id) / f"{name}.json"
        write_private_json_exclusive(path, value)
        receipt = self._archive.put_json(
            key=f"learning/v5/protected-studies/{run_id}/{name}.json",
            payload=dict(value),
        )
        self._write_archive_receipt(run_id=run_id, name=name, receipt=receipt)

    def _write_archive_receipt(
        self, *, run_id: str, name: str, receipt: Mapping[str, Any]
    ) -> None:
        write_private_json_exclusive(
            self._archive_directory(run_id) / f"{name}.json",
            dict(receipt),
        )

    def _audit_head(self, run_id: str) -> str | None:
        events = self.audit_events(run_id=run_id)
        return str(events[-1]["event_sha256"]) if events else None

    def _run_directory(self, run_id: str) -> pathlib.Path:
        return self._directory / "runs" / run_id

    def _events_directory(self, run_id: str) -> pathlib.Path:
        return self._run_directory(run_id) / "events"

    def _archive_directory(self, run_id: str) -> pathlib.Path:
        return self._run_directory(run_id) / "archive-receipts"


class ProtectedEvidenceArchive:
    """Write-once KMS-encrypted protected artifacts with exact-version verification."""

    def __init__(self, *, bucket: str, kms_key_id: str, client: Any) -> None:
        if not bucket or "/" in bucket or not kms_key_id:
            raise ValueError("v5 protected evidence archive identity is invalid")
        self.bucket = bucket
        self.kms_key_id = kms_key_id
        self.client = client

    def put_json(self, *, key: str, payload: Any) -> dict[str, Any]:
        if not key.startswith("learning/v5/protected-studies/") or ".." in key.split("/"):
            raise ValueError("v5 protected evidence key is invalid")
        body = canonical_json_bytes(payload)
        digest = hashlib.sha256(body).hexdigest()
        checksum = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        created = False
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self.kms_key_id,
                BucketKeyEnabled=True,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=checksum,
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
            version_id = str(response.get("VersionId") or "")
            created = True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            status = int(
                exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
            )
            # S3's generated exception map does not expose PreconditionFailed.
            if code not in {"PreconditionFailed", "412"} and status != 412:
                raise
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            version_id = str(head.get("VersionId") or "")
        if not version_id:
            raise RuntimeError("v5 protected archive returned no object version")
        response = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version_id)
        stream = response["Body"]
        try:
            observed = stream.read()
        finally:
            stream.close()
        if observed != body:
            raise RuntimeError("v5 protected archive object differs from expected content")
        retention = (
            self.client.get_object_retention(
                Bucket=self.bucket,
                Key=key,
                VersionId=version_id,
            ).get("Retention")
            or {}
        )
        retain_until = retention.get("RetainUntilDate")
        if retention.get("Mode") != "GOVERNANCE" or retain_until is None:
            raise RuntimeError("v5 protected archive object has no Governance retention")
        return {
            "bucket": self.bucket,
            "key": key,
            "version_id": version_id,
            "sha256": digest,
            "checksum_sha256": checksum,
            "kms_key_id": self.kms_key_id,
            "retain_until": retain_until.isoformat(),
            "created": created,
        }

    def put_audit_event(self, *, run_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        sequence = int(event.get("sequence") or 0)
        if sequence < 1:
            raise ValueError("v5 protected audit archive sequence is invalid")
        return self.put_json(
            key=(
                f"learning/v5/protected-studies/{run_id}/audit/"
                f"{sequence:08d}-{event['event_sha256']}.json"
            ),
            payload=dict(event),
        )

class MonitorLease:
    """A small fail-closed liveness lease checked before irreversible steps."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("v5 protected monitor timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._last_heartbeat = time.monotonic()
        self._available = True
        self._lock = threading.Lock()

    def heartbeat(self) -> None:
        with self._lock:
            if not self._available:
                raise RuntimeError("v5 protected monitor is unavailable")
            self._last_heartbeat = time.monotonic()

    def stop(self) -> None:
        with self._lock:
            self._available = False

    def require_live(self) -> None:
        with self._lock:
            age = time.monotonic() - self._last_heartbeat
            if not self._available or age > self._timeout_seconds:
                raise RuntimeError("v5 protected monitoring outage")


class LeaseHeartbeat:
    """Refresh a monitor lease only while its independent health check succeeds."""

    def __init__(
        self,
        *,
        lease: MonitorLease,
        health_check: Callable[[], None],
        interval_seconds: float = 2.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("v5 protected heartbeat interval must be positive")
        self._lease = lease
        self._health_check = health_check
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> LeaseHeartbeat:
        self._health_check()
        self._lease.heartbeat()
        self._thread = threading.Thread(
            target=self._run,
            name="v5-protected-monitor",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._interval * 2))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._health_check()
                self._lease.heartbeat()
            except Exception:
                self._lease.stop()
                return


class BehavioralStudyRunner:
    """Execute frozen paired trials while exposing progress but not live outcomes."""

    def __init__(
        self,
        *,
        run_id: str,
        final_freeze_sha256: str,
        reasoning_provider: ReasoningProvider,
        context_provider: ArmContextProvider,
        audit_store: ProtectedAuditStore,
        monitor: MonitorLease,
        stop_on_unsafe: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._freeze_sha = final_freeze_sha256
        self._reasoning = reasoning_provider
        self._contexts = context_provider
        self._audit = audit_store
        self._monitor = monitor
        self._stop_on_unsafe = stop_on_unsafe
        self._progress = progress_callback

    def run(self, *, scenarios: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        total = len(scenarios) * PROTECTED_REPETITIONS * len(PROTECTED_ARMS)
        trials: list[dict[str, Any]] = []
        completed = 0
        for scenario in scenarios:
            scenario_id = str(scenario["scenario_id"])
            for repetition in range(1, PROTECTED_REPETITIONS + 1):
                for arm in deterministic_arm_order(
                    final_freeze_sha256=self._freeze_sha,
                    scenario_id=scenario_id,
                    repetition=repetition,
                ):
                    self._require_monitor()
                    trial = self._run_trial(
                        scenario=scenario,
                        repetition=repetition,
                        arm=arm,
                    )
                    trials.append(trial)
                    completed += 1
                    self._audit.append(
                        run_id=self._run_id,
                        category="monitoring",
                        payload={
                            "event": "trial_progress",
                            "completed": completed,
                            "total": total,
                            "monitor_state": "healthy",
                        },
                    )
                    if self._progress is not None:
                        self._progress(completed, total)
        return trials

    def _run_trial(
        self,
        *,
        scenario: Mapping[str, Any],
        repetition: int,
        arm: str,
    ) -> dict[str, Any]:
        scenario_id = str(scenario["scenario_id"])
        trial_id = sha256_hex(
            [self._run_id, self._freeze_sha, scenario_id, repetition, arm]
        )
        simulator = V5IncidentSimulator(dict(scenario))
        for step in range(1, ACTION_BUDGET + 1):
            self._require_monitor()
            context = dict(
                self._contexts.context(
                    scenario=scenario,
                    arm=arm,
                    trial_id=trial_id,
                    step=step,
                )
            )
            if context.get("hard_gate_passed") is not True:
                raise ProtectedRunFailure(
                    "hard_gate_failure",
                    "v5 protected retrieval context failed a hard gate",
                )
            retrieval = dict(context.get("retrieval") or {})
            self._audit.append(
                run_id=self._run_id,
                category="retrieval_decision",
                payload={
                    "trial_id": trial_id,
                    "step": step,
                    **retrieval,
                },
            )
            for read in list(context.get("reads") or []):
                self._audit.append(
                    run_id=self._run_id,
                    category="memory_read",
                    payload={"trial_id": trial_id, "step": step, **dict(read)},
                )
            prompt_context: dict[str, Any] = {
                "incident": scenario["agent_view"]["recurrence"]["incident"],
                "observation": simulator.observe(),
                "memories": [
                    {"content": str(row.get("content") or "")}
                    for row in list(context.get("memories") or [])
                ],
                "step": step,
            }
            if arm == "reference_lesson":
                prompt_context["reference_control"] = reference_lesson(scenario)
            try:
                response = self._reasoning.generate(
                    ReasoningRequest(
                        system=ACTION_SYSTEM_PROMPT,
                        prompt=json.dumps(prompt_context, sort_keys=True),
                        temperature=0.0,
                        max_output_tokens=256,
                        routing_key=f"{self._freeze_sha}:{trial_id}:{step}",
                        response_json_schema=ACTION_RESPONSE_SCHEMA,
                        thinking_budget=0,
                    )
                )
            except Exception as exc:
                raise ProtectedRunFailure(
                    "artifact_integrity_failure",
                    "v5 protected reasoning provider failed",
                ) from exc
            if response.provider != self._reasoning.provider_name or response.model != self._reasoning.model_name:
                raise ProtectedRunFailure(
                    "artifact_integrity_failure",
                    "v5 protected reasoning provider identity drifted",
                )
            action = _parse_action(response.text)
            response_record = {
                "trial_id": trial_id,
                "step": step,
                "provider": response.provider,
                "model": response.model,
                "response_text": response.text,
                "response_sha256": sha256_hex(response.text),
                "usage": dict(response.usage),
                "action": action,
            }
            self._audit.append(
                run_id=self._run_id,
                category="reasoning_response",
                payload=response_record,
            )
            self._require_monitor()
            try:
                outcome = simulator.step(action)
            except Exception as exc:
                raise ProtectedRunFailure(
                    "hard_gate_failure",
                    "v5 protected simulator action failed",
                ) from exc
            self._audit.append(
                run_id=self._run_id,
                category="outcome",
                payload={
                    "trial_id": trial_id,
                    "scenario_id": scenario_id,
                    "repetition": repetition,
                    "arm": arm,
                    "step": step,
                    "action": action,
                    "outcome": outcome,
                },
            )
            if outcome.get("unsafe") is True and self._stop_on_unsafe:
                raise ProtectedRunFailure(
                    "safety_failure",
                    "v5 protected simulator observed an unsafe action",
                )
            if simulator.recovered or action == "stop":
                break
        score = simulator.score()
        trial = {
            "trial_id": trial_id,
            "scenario_id": scenario_id,
            "repetition": repetition,
            "arm": arm,
            **score,
        }
        self._audit.append(
            run_id=self._run_id,
            category="outcome",
            payload={"event": "trial_terminal", **trial},
        )
        return trial

    def _require_monitor(self) -> None:
        try:
            self._monitor.require_live()
        except RuntimeError as exc:
            raise ProtectedRunFailure("monitoring_outage", str(exc)) from exc


class DatabaseArmContextProvider:
    """Retrieve frozen arm context through the production CockroachDB path."""

    def __init__(
        self,
        *,
        db_url: str,
        provider: CheckpointedEmbeddingProvider,
        prepared_cases: Mapping[str, Mapping[str, Any]],
        cache_only_delegate: CacheOnlyEmbeddingProvider,
        phase: Literal["development-pilot", "protected"] = "protected",
        store_factory: Callable[..., QualificationStore] = MemoryStore,
    ) -> None:
        self._db_url = db_url
        self._provider = provider
        self._cases = prepared_cases
        self._cache_only = cache_only_delegate
        self._phase = phase
        self._store_factory = store_factory

    def context(
        self,
        *,
        scenario: Mapping[str, Any],
        arm: str,
        trial_id: str,
        step: int,
    ) -> Mapping[str, Any]:
        scenario_id = str(scenario["scenario_id"])
        case = self._cases[scenario_id]
        namespace = str(case["namespaces"][arm])
        target_id = case["target_database_ids"].get(arm)
        query = render_retrieval_query(scenario)
        decision_id = f"v5-{self._phase}:{trial_id}:{step}"
        misses_before = self._cache_only.miss_count
        try:
            with self._store_factory(url=self._db_url, embedding_provider=self._provider) as store:
                with self._provider.query_scope(scenario_id=scenario_id, query=query):
                    retrieval = store.retrieve_semantic(
                        namespace=namespace,
                        query=query,
                        decision_id=decision_id,
                        reader=f"v5.{self._phase.replace('-', '_')}.agent",
                        purpose=f"Choose one externally scored {self._phase} simulator action",
                        policy="semantic_strict",
                        limit=4,
                        positive_guidance_only=True,
                    )
                _verify_retrieval_trace(store=store, retrieval=retrieval, decision_id=decision_id)
                _seal_retrieval_decision(store=store, decision_id=decision_id)
                hits = list(_result_hits(retrieval))
                reads = store.reads_for_decision(decision_id=decision_id)
        except Exception as exc:
            if self._cache_only.miss_count > misses_before:
                raise ProtectedRunFailure(
                    "embedding_cache_miss",
                    "v5 protected execution observed an embedding cache miss",
                ) from exc
            raise
        returned_ids = [str(row["id"]) for row in hits]
        known_membership = set(returned_ids).issubset(set(case["arm_database_ids"][arm]))
        target_rank_one = target_id is None or (bool(returned_ids) and returned_ids[0] == target_id)
        target_absent = target_id is not None or case["consolidated_target_id"] not in returned_ids
        hard_gate = _behavioral_retrieval_hard_gate(
            policy=_result_value(retrieval, "policy"),
            fallback_reason=_result_value(retrieval, "fallback_reason"),
            target_rank_one=target_rank_one,
            target_absent=target_absent,
            known_membership=known_membership,
        )
        return {
            "hard_gate_passed": hard_gate,
            "memories": hits,
            "retrieval": {
                "retrieval_id": str(_result_value(retrieval, "retrieval_id") or ""),
                "decision_id": decision_id,
                "arm": arm,
                "policy": _result_value(retrieval, "policy"),
                "fallback_reason": _result_value(retrieval, "fallback_reason"),
                "returned_memory_ids": returned_ids,
                "target_rank_one": target_rank_one,
                "target_absent": target_absent,
                "known_arm_membership": known_membership,
                "decision_sealed": True,
            },
            "reads": [
                {
                    "retrieval_id": str(row.get("retrieval_id") or ""),
                    "memory_id": str(
                        row.get("semantic_memory_id") or row.get("memory_id") or ""
                    ),
                    "rank": int(row.get("rank") or 0),
                }
                for row in reads
            ],
        }


def prepare_arm_database(
    *,
    scenarios: Sequence[Mapping[str, Any]],
    db_url: str,
    provider: CheckpointedEmbeddingProvider,
    execution_contract_sha256: str,
    phase: Literal["development-pilot", "protected"] = "protected",
    store_factory: Callable[..., QualificationStore] = MemoryStore,
) -> dict[str, dict[str, Any]]:
    """Load identical non-target context and the consolidated target into isolated arms."""

    prepared: dict[str, dict[str, Any]] = {}
    with store_factory(url=db_url, embedding_provider=provider) as store:
        for scenario in scenarios:
            scenario_id = str(scenario["scenario_id"])
            recurrence = scenario["agent_view"]["recurrence"]
            memories = list(_candidate_memories(scenario))
            matching = [
                memory
                for memory in memories
                if _candidate_is_positive(memory)
                and applicability_matches(
                    dict(memory["applicability"]),
                    service=str(recurrence["service"]),
                    workload=str(recurrence["workload"]),
                    observations=dict(recurrence["initial_observation"]),
                )
            ]
            if len(matching) != 1:
                raise ValueError("v5 protected arm preparation requires one target")
            target_memory_id = str(matching[0]["memory_id"])
            namespaces = {
                arm: f"v5-{phase}-{scenario_id}-{arm.replace('_', '-')}"
                for arm in PROTECTED_ARMS
            }
            target_database_ids: dict[str, str | None] = {}
            arm_database_ids: dict[str, list[str]] = {}
            consolidated_target_id = ""
            for arm in PROTECTED_ARMS:
                target_database_ids[arm] = None
                arm_database_ids[arm] = []
                selected = (
                    memories
                    if arm == "consolidated_lesson"
                    else [row for row in memories if row["memory_id"] != target_memory_id]
                )
                for memory in selected:
                    vector = provider.embed_document(render_retrieval_document(memory))
                    governance = MemoryGovernance(
                        operator_disposition=str(memory["operator_disposition"]),  # type: ignore[arg-type]
                        safety_status=str(memory["safety_status"]),  # type: ignore[arg-type]
                        contradiction_status=str(memory["contradiction_status"]),  # type: ignore[arg-type]
                        usage_instruction=str(memory["usage_instruction"]),  # type: ignore[arg-type]
                    )
                    content = str(memory["content"])
                    document = render_retrieval_document(memory)
                    row = _remember_candidate_with_retry(
                        store=store,
                        memory_kind="semantic",
                        namespace=namespaces[arm],
                        content=content,
                        provenance=Provenance(
                            writer=f"v5.{phase.replace('-', '_')}.preparation",
                            source_ref=f"v5-{phase}:{scenario_id}:{arm}:{memory['memory_id']}",
                            justification=f"Prepare one frozen {phase} arm candidate",
                        ),
                        metadata=candidate_database_metadata(memory),
                        content_schema=f"v5_{phase.replace('-', '_')}_candidate.v1",
                        structured_payload=candidate_database_payload(
                            scenario_id=scenario_id,
                            candidate_id=str(memory["memory_id"]),
                            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                            document_sha256=hashlib.sha256(document.encode("utf-8")).hexdigest(),
                            envelope_sha256=sha256_hex(dict(memory)),
                            qualification_contract_sha256=execution_contract_sha256,
                        ),
                        precomputed_embedding=vector,
                        trust_status=(
                            "review_required"
                            if memory["status"] == "review_required"
                            else "active"
                        ),
                        governance=governance,
                    )
                    database_id = str(row["id"])
                    arm_database_ids[arm].append(database_id)
                    if memory["memory_id"] == target_memory_id:
                        target_database_ids[arm] = database_id
                        consolidated_target_id = database_id
                    if memory["status"] == "invalidated":
                        store.invalidate(
                            memory_id=database_id,
                            memory_kind="semantic",
                            actor=f"v5.{phase.replace('-', '_')}.preparation",
                            reason=f"Frozen {phase} candidate invalidation state",
                        )
            if not consolidated_target_id:
                raise RuntimeError("v5 protected consolidated target was not prepared")
            prepared[scenario_id] = {
                "namespaces": namespaces,
                "target_database_ids": target_database_ids,
                "arm_database_ids": arm_database_ids,
                "consolidated_target_id": consolidated_target_id,
            }
    return prepared


def rollback_class(reason: str | None) -> str:
    try:
        return ROLLBACK_CLASS[str(reason)]
    except KeyError as exc:
        raise ValueError("v5 protected rollback reason is not frozen") from exc


def _parse_action(text: str) -> str:
    candidate = text.strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ProtectedRunFailure(
            "artifact_integrity_failure",
            "v5 protected reasoning response is not JSON",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"action"}:
        raise ProtectedRunFailure(
            "artifact_integrity_failure",
            "v5 protected reasoning response shape differs",
        )
    action = payload["action"]
    if not isinstance(action, str) or not _ACTION_RE.fullmatch(action):
        raise ProtectedRunFailure(
            "artifact_integrity_failure",
            "v5 protected reasoning action is invalid",
        )
    if action not in ACTION_RESPONSE_SCHEMA["properties"]["action"]["enum"]:
        raise ProtectedRunFailure(
            "hard_gate_failure",
            "v5 protected reasoning action is outside the frozen vocabulary",
        )
    return action


def _run_id(*, run_kind: str, authorization_sha256: str, contract_sha256: str) -> str:
    value = f"https://hindsight.local/v5/protected/{run_kind}/{authorization_sha256}/{contract_sha256}"
    return str(uuid5(NAMESPACE_URL, value))


def _event_body(
    *,
    run_id: str,
    sequence: int,
    category: str,
    payload: Mapping[str, Any],
    previous_event_sha256: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "sequence": sequence,
        "category": category,
        "payload": dict(payload),
        "previous_event_sha256": previous_event_sha256,
    }


def _load_json_file(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("v5 protected local record is invalid")
    return value


__all__ = [
    "LeaseHeartbeat",
    "MonitorLease",
    "BehavioralStudyRunner",
    "DatabaseArmContextProvider",
    "ProtectedRunFailure",
    "ProtectedAuditStore",
    "ProtectedEvidenceArchive",
    "prepare_arm_database",
    "rollback_class",
]
