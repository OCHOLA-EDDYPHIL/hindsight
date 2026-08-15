"""Run the pinned governed-memory audit and restricted-role denial probes."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hindsight.db import database_url  # noqa: E402
from hindsight.infrastructure_auditor import (  # noqa: E402
    AUDITOR_ROLE,
    run_infrastructure_audit,
)

RUN_SCHEMA_VERSION = "hindsight.infrastructure-audit-run.v2"
REPEATED_RECEIPT_SCHEMA_VERSION = "hindsight.infrastructure-audit-receipt.v1"
DENIAL_RECEIPT_SCHEMA_VERSION = "hindsight.infrastructure-audit-denials.v1"
INSUFFICIENT_PRIVILEGE_SQLSTATE = "42501"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DenialProbe:
    probe_id: str
    category: str
    statement: str
    tenant_parameter: bool = False


DENIAL_PROBES = (
    DenialProbe(
        "insert",
        "dml",
        """
        INSERT INTO demo_sessions (
            id, demo_kind, namespace, created_by, tenant_id
        )
        SELECT
            gen_random_uuid(), 'infrastructure-audit-probe',
            'infrastructure-audit-probe', 'infrastructure-audit',
            %s::UUID
        WHERE false
        """,
        tenant_parameter=True,
    ),
    DenialProbe(
        "update",
        "dml",
        "UPDATE demo_sessions SET status = status WHERE false",
    ),
    DenialProbe(
        "delete",
        "dml",
        "DELETE FROM demo_sessions WHERE false",
    ),
    DenialProbe(
        "ddl",
        "ddl",
        "CREATE TABLE public.infrastructure_auditor_denial_probe (id INT PRIMARY KEY)",
    ),
    DenialProbe(
        "grant",
        "grant",
        "GRANT hindsight_agent_writer TO root",
    ),
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_bytes(value)
    return sha256(payload).hexdigest()


def _overall_status(statuses: Iterable[object]) -> str:
    observed = [str(status) for status in statuses]
    allowed = {"PASS", "WARN", "FAIL", "UNAVAILABLE"}
    if not observed or any(status not in allowed for status in observed):
        return "UNAVAILABLE"
    for status in ("FAIL", "WARN", "UNAVAILABLE"):
        if status in observed:
            return status
    return "PASS"


def _scenario_id(value: str) -> str:
    try:
        parsed = str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scenario id must be an exact UUID") from exc
    if value != parsed:
        raise argparse.ArgumentTypeError("scenario id must be an exact UUID")
    return parsed


def resolve_scenario_namespace(
    *,
    db_url: str,
    tenant_id: str,
    scenario_id: str,
    connect: Callable[..., Any] = psycopg.connect,
) -> str:
    """Resolve one tenant-bound demo session without exposing its namespace."""

    with connect(db_url) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute(
            "SELECT set_config('hindsight.tenant_id', %s, true)",
            (tenant_id,),
        )
        rows = connection.execute(
            """
            SELECT namespace
            FROM demo_sessions
            WHERE id = %s::UUID
              AND tenant_id = %s::UUID
              AND tenant_id = current_hindsight_tenant_id()
            """,
            (scenario_id, tenant_id),
        ).fetchall()
    if len(rows) != 1 or len(rows[0]) != 1:
        raise RuntimeError("scenario id did not resolve to exactly one tenant-bound namespace")
    namespace = rows[0][0]
    if not isinstance(namespace, str) or not namespace.strip():
        raise RuntimeError("scenario id did not resolve to exactly one tenant-bound namespace")
    return namespace


def _probe_result(
    probe: DenialProbe,
    *,
    status: str,
    code: str,
    observed_sqlstate: str | None,
    transaction_rolled_back: bool,
) -> dict[str, Any]:
    statement_sha256 = _digest(probe.statement.strip().encode("utf-8"))
    result = {
        "id": probe.probe_id,
        "category": probe.category,
        "statement_sha256": statement_sha256,
        "status": status,
        "code": code,
        "expected_sqlstate": INSUFFICIENT_PRIVILEGE_SQLSTATE,
        "observed_sqlstate": observed_sqlstate,
        "transaction_rolled_back": transaction_rolled_back,
    }
    return {
        **result,
        "result_sha256": _digest(result),
    }


def _run_denial_probe(
    probe: DenialProbe,
    *,
    db_url: str,
    tenant_id: str,
    connect: Callable[..., Any],
) -> dict[str, Any]:
    connection = None
    status = "UNAVAILABLE"
    code = "probe_connection_unavailable"
    observed_sqlstate = None
    transaction_rolled_back = False
    try:
        connection = connect(db_url, autocommit=False)
        try:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(AUDITOR_ROLE)))
            connection.execute(
                "SELECT set_config('hindsight.tenant_id', %s, true)",
                (tenant_id,),
            )
            identity = connection.execute("SELECT current_user::STRING").fetchone()
        except psycopg.Error as exc:
            code = "probe_setup_unavailable"
            observed_sqlstate = exc.sqlstate
        else:
            if identity != (AUDITOR_ROLE,):
                status = "FAIL"
                code = "unexpected_effective_role"
            else:
                try:
                    params = (tenant_id,) if probe.tenant_parameter else None
                    connection.execute(probe.statement, params)
                except psycopg.errors.InsufficientPrivilege as exc:
                    observed_sqlstate = exc.sqlstate
                    if observed_sqlstate == INSUFFICIENT_PRIVILEGE_SQLSTATE:
                        status = "PASS"
                        code = "insufficient_privilege"
                    else:  # pragma: no cover - psycopg fixes SQLSTATE by exception class
                        code = "probe_result_unavailable"
                except psycopg.Error as exc:
                    code = "probe_result_unavailable"
                    observed_sqlstate = exc.sqlstate
                else:
                    status = "FAIL"
                    code = "forbidden_statement_succeeded"
    except psycopg.Error as exc:
        observed_sqlstate = exc.sqlstate
    finally:
        if connection is not None:
            try:
                connection.rollback()
                transaction_rolled_back = True
            except psycopg.Error as exc:
                raise RuntimeError("infrastructure denial probe rollback failed") from exc
            finally:
                connection.close()
    return _probe_result(
        probe,
        status=status,
        code=code,
        observed_sqlstate=observed_sqlstate,
        transaction_rolled_back=transaction_rolled_back,
    )


def run_denial_probes(
    *,
    db_url: str,
    tenant_id: str,
    connect: Callable[..., Any] = psycopg.connect,
) -> dict[str, Any]:
    """Prove that the effective auditor role cannot mutate or grant privileges."""

    results = [
        _run_denial_probe(
            probe,
            db_url=db_url,
            tenant_id=tenant_id,
            connect=connect,
        )
        for probe in DENIAL_PROBES
    ]
    receipt = {
        "schema_version": DENIAL_RECEIPT_SCHEMA_VERSION,
        "auditor_role": AUDITOR_ROLE,
        "scope": {"tenant_id_sha256": _digest(tenant_id.encode("utf-8"))},
        "status": _overall_status(result["status"] for result in results),
        "results": results,
    }
    conclusion = {
        "schema_version": receipt["schema_version"],
        "auditor_role": receipt["auditor_role"],
        "scope": receipt["scope"],
        "status": receipt["status"],
        "results": [
            {
                "id": result["id"],
                "category": result["category"],
                "statement_sha256": result["statement_sha256"],
                "status": result["status"],
                "code": result["code"],
                "expected_sqlstate": result["expected_sqlstate"],
                "observed_sqlstate": result["observed_sqlstate"],
                "transaction_rolled_back": result["transaction_rolled_back"],
                "result_sha256": result["result_sha256"],
            }
            for result in results
        ],
    }
    receipt["conclusion_sha256"] = _digest(conclusion)
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def _repeated_receipt(
    *,
    source_revision: str,
    catalog_receipt: dict[str, Any],
    denial_receipt: dict[str, Any],
) -> dict[str, Any]:
    if catalog_receipt.get("source_revision") != source_revision:
        raise RuntimeError("infrastructure audit receipt does not bind the requested revision")
    catalog_scope = catalog_receipt.get("scope")
    denial_scope = denial_receipt.get("scope")
    if (
        catalog_receipt.get("auditor_role") != AUDITOR_ROLE
        or denial_receipt.get("auditor_role") != AUDITOR_ROLE
    ):
        raise RuntimeError("infrastructure audit receipt does not bind the auditor role")
    if (
        not isinstance(catalog_scope, dict)
        or not isinstance(denial_scope, dict)
        or catalog_scope.get("tenant_id_sha256") != denial_scope.get("tenant_id_sha256")
    ):
        raise RuntimeError("infrastructure audit receipt scopes do not match")
    status = _overall_status((catalog_receipt.get("status"), denial_receipt.get("status")))
    conclusion = {
        "schema_version": REPEATED_RECEIPT_SCHEMA_VERSION,
        "source_revision": source_revision,
        "catalog_conclusion_sha256": catalog_receipt["conclusion_sha256"],
        "denial_conclusion_sha256": denial_receipt["conclusion_sha256"],
        "status": status,
    }
    receipt = {
        **conclusion,
        "catalog_receipt": catalog_receipt,
        "denial_receipt": denial_receipt,
        "conclusion_sha256": _digest(conclusion),
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def build_audit_run(
    *,
    db_url: str,
    tenant_id: str,
    namespace: str,
    source_revision: str,
    repeat: int,
    connect: Callable[..., Any] = psycopg.connect,
    audit_runner: Callable[..., dict[str, Any]] = run_infrastructure_audit,
    denial_runner: Callable[..., dict[str, Any]] = run_denial_probes,
) -> dict[str, Any]:
    receipts: list[dict[str, Any]] = []
    for _ in range(repeat):
        with connect(db_url) as connection:
            catalog_receipt = audit_runner(
                connection,
                tenant_id=tenant_id,
                namespace=namespace,
                source_revision=source_revision,
            )
        denial_receipt = denial_runner(
            db_url=db_url,
            tenant_id=tenant_id,
            connect=connect,
        )
        receipts.append(
            _repeated_receipt(
                source_revision=source_revision,
                catalog_receipt=catalog_receipt,
                denial_receipt=denial_receipt,
            )
        )

    conclusions = {str(receipt["conclusion_sha256"]) for receipt in receipts}
    conclusions_match = len(conclusions) == 1
    receipt_identities = {str(receipt["receipt_sha256"]) for receipt in receipts}
    receipts_match = len(receipt_identities) == 1
    document = {
        "schema_version": RUN_SCHEMA_VERSION,
        "source_revision": source_revision,
        "repeat_count": repeat,
        "conclusions_match": conclusions_match,
        "conclusion_sha256": receipts[0]["conclusion_sha256"] if conclusions_match else None,
        "receipts_match": receipts_match,
        "repeated_receipt_sha256": (
            receipts[0]["receipt_sha256"] if receipts_match else None
        ),
        "status": _overall_status(receipt["status"] for receipt in receipts)
        if conclusions_match and receipts_match
        else "FAIL",
        "receipts": receipts,
    }
    document["receipt_sha256"] = _digest(document)
    return document


def _require_exact_acceptance(document: dict[str, Any]) -> None:
    if document.get("repeat_count") != 2:
        raise RuntimeError("infrastructure audit exact acceptance requires two runs")
    if document.get("conclusions_match") is not True:
        raise RuntimeError("repeated infrastructure audits produced different conclusions")
    receipts = document.get("receipts")
    if not isinstance(receipts, list) or len(receipts) != 2:
        raise RuntimeError("infrastructure audit exact acceptance requires two receipts")
    receipt_hashes = [
        receipt.get("receipt_sha256") if isinstance(receipt, dict) else None for receipt in receipts
    ]
    if (
        document.get("receipts_match") is not True
        or not all(
            isinstance(value, str) and SHA256_PATTERN.fullmatch(value) for value in receipt_hashes
        )
        or len(set(receipt_hashes)) != 1
        or document.get("repeated_receipt_sha256") != receipt_hashes[0]
    ):
        raise RuntimeError("repeated infrastructure audits produced different full receipts")
    if document.get("status") != "PASS":
        raise RuntimeError(f"infrastructure audit exact acceptance was {document.get('status')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--namespace")
    scope.add_argument("--scenario-id", type=_scenario_id)
    parser.add_argument(
        "--source-revision",
        default=os.environ.get("HINDSIGHT_DEPLOYED_REVISION", ""),
        required=False,
    )
    parser.add_argument("--repeat", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.source_revision:
        raise RuntimeError("--source-revision or HINDSIGHT_DEPLOYED_REVISION is required")

    resolved_database_url = database_url()
    namespace = args.namespace
    if args.scenario_id is not None:
        namespace = resolve_scenario_namespace(
            db_url=resolved_database_url,
            tenant_id=args.tenant_id,
            scenario_id=args.scenario_id,
        )
    if namespace is None:  # pragma: no cover - argparse enforces the scope group
        raise RuntimeError("an infrastructure audit namespace is required")

    document = build_audit_run(
        db_url=resolved_database_url,
        tenant_id=args.tenant_id,
        namespace=namespace,
        source_revision=args.source_revision,
        repeat=args.repeat,
    )
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized)
    print(serialized, end="")
    _require_exact_acceptance(document)


if __name__ == "__main__":
    main()
