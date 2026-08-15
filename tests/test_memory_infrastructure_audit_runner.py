"""Exact-acceptance contracts for the infrastructure-audit runner."""

from __future__ import annotations

from hashlib import sha256
import json

import psycopg
import pytest
from psycopg import sql

from scripts import run_memory_infrastructure_audit as runner


class _Result:
    def __init__(self, *, one=None, rows=()):
        self.one = one
        self.rows = rows

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class _DenialConnection:
    def __init__(self, *, outcome: str = "denied"):
        self.outcome = outcome
        self.calls = []
        self.rolled_back = False
        self.closed = False

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        if isinstance(statement, sql.Composed):
            if self.outcome == "setup-denied":
                raise psycopg.errors.InsufficientPrivilege("setup denied")
            return _Result()
        if str(statement).startswith("SELECT set_config"):
            return _Result()
        if statement == "SELECT current_user::STRING":
            if self.outcome == "wrong-role":
                return _Result(one=("unexpected-role",))
            return _Result(one=(runner.AUDITOR_ROLE,))
        if self.outcome == "denied":
            raise psycopg.errors.InsufficientPrivilege("statement denied")
        if self.outcome == "unavailable":
            raise psycopg.errors.UndefinedTable("probe unavailable")
        return _Result()

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _denial_factory(outcome: str = "denied"):
    connections = []

    def connect(_url, *, autocommit):
        assert autocommit is False
        connection = _DenialConnection(outcome=outcome)
        connections.append(connection)
        return connection

    return connect, connections


def test_denial_receipt_is_deterministic_redacted_and_rolled_back():
    tenant_id = "00000000-0000-0000-0000-000000000001"
    db_url = "postgresql://deploy:forbidden@example.invalid/hindsight"
    first_connect, first_connections = _denial_factory()
    second_connect, second_connections = _denial_factory()

    first = runner.run_denial_probes(
        db_url=db_url,
        tenant_id=tenant_id,
        connect=first_connect,
    )
    second = runner.run_denial_probes(
        db_url=db_url,
        tenant_id=tenant_id,
        connect=second_connect,
    )

    assert first == second
    assert first["status"] == "PASS"
    assert [result["id"] for result in first["results"]] == [
        "insert",
        "update",
        "delete",
        "ddl",
        "grant",
    ]
    assert all(
        result["status"] == "PASS"
        and result["code"] == "insufficient_privilege"
        and result["observed_sqlstate"] == runner.INSUFFICIENT_PRIVILEGE_SQLSTATE
        and result["transaction_rolled_back"] is True
        and len(result["statement_sha256"]) == 64
        and len(result["result_sha256"]) == 64
        for result in first["results"]
    )
    assert first["scope"] == {"tenant_id_sha256": sha256(tenant_id.encode()).hexdigest()}
    assert len({result["result_sha256"] for result in first["results"]}) == 5
    assert all(connection.rolled_back and connection.closed for connection in first_connections)
    assert all(connection.rolled_back and connection.closed for connection in second_connections)
    serialized = json.dumps(first, sort_keys=True)
    assert tenant_id not in serialized
    assert db_url not in serialized
    assert "postgresql://" not in serialized
    assert all(probe.statement.strip() not in serialized for probe in runner.DENIAL_PROBES)


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_code"),
    [
        ("allowed", "FAIL", "forbidden_statement_succeeded"),
        ("wrong-role", "FAIL", "unexpected_effective_role"),
        ("unavailable", "UNAVAILABLE", "probe_result_unavailable"),
        ("setup-denied", "UNAVAILABLE", "probe_setup_unavailable"),
    ],
)
def test_denial_probes_fail_closed(outcome, expected_status, expected_code):
    connect, connections = _denial_factory(outcome)

    receipt = runner.run_denial_probes(
        db_url="postgresql://example.invalid/hindsight",
        tenant_id="00000000-0000-0000-0000-000000000001",
        connect=connect,
    )

    assert receipt["status"] == expected_status
    assert {result["status"] for result in receipt["results"]} == {expected_status}
    assert {result["code"] for result in receipt["results"]} == {expected_code}
    assert all(connection.rolled_back and connection.closed for connection in connections)


class _ScenarioConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        if "SELECT namespace" in statement:
            return _Result(rows=self.rows)
        return _Result()


def test_scenario_namespace_resolution_is_exact_and_tenant_bound():
    scenario_id = "49109a44-43e7-40de-b547-b4f9d0a387a2"
    tenant_id = "00000000-0000-0000-0000-000000000002"
    connection = _ScenarioConnection([("private-live-namespace",)])

    namespace = runner.resolve_scenario_namespace(
        db_url="postgresql://example.invalid/hindsight",
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        connect=lambda _url: connection,
    )

    assert namespace == "private-live-namespace"
    query, params = connection.calls[-1]
    assert "tenant_id = %s::UUID" in query
    assert "tenant_id = current_hindsight_tenant_id()" in query
    assert params == (scenario_id, tenant_id)


@pytest.mark.parametrize(
    "value",
    [
        "49109A44-43E7-40DE-B547-B4F9D0A387A2",
        "49109a4443e740deb547b4f9d0a387a2",
        "not-a-uuid",
    ],
)
def test_scenario_id_requires_canonical_uuid(value):
    with pytest.raises(runner.argparse.ArgumentTypeError, match="exact UUID"):
        runner._scenario_id(value)  # noqa: SLF001


@pytest.mark.parametrize(
    "rows",
    [[], [("private-one",), ("private-two",)], [(None,)], [("",)]],
)
def test_scenario_namespace_resolution_requires_exactly_one_match(rows):
    connection = _ScenarioConnection(rows)

    with pytest.raises(RuntimeError) as raised:
        runner.resolve_scenario_namespace(
            db_url="postgresql://example.invalid/hindsight",
            tenant_id="00000000-0000-0000-0000-000000000002",
            scenario_id="49109a44-43e7-40de-b547-b4f9d0a387a2",
            connect=lambda _url: connection,
        )

    assert "private-one" not in str(raised.value)
    assert "private-two" not in str(raised.value)


class _AuditConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _catalog_receipt(source_revision: str, *, status: str = "PASS"):
    return {
        "schema_version": "hindsight.infrastructure-audit.v1",
        "source_revision": source_revision,
        "auditor_role": runner.AUDITOR_ROLE,
        "official_skill": {"commit": "pinned"},
        "scope": {
            "tenant_id_sha256": "a" * 64,
            "namespace_sha256": "b" * 64,
        },
        "status": status,
        "conclusion_sha256": "c" * 64,
        "receipt_sha256": "d" * 64,
    }


def _denial_receipt(*, status: str = "PASS"):
    return {
        "schema_version": runner.DENIAL_RECEIPT_SCHEMA_VERSION,
        "auditor_role": runner.AUDITOR_ROLE,
        "scope": {"tenant_id_sha256": "a" * 64},
        "status": status,
        "conclusion_sha256": "e" * 64,
        "receipt_sha256": "f" * 64,
    }


def _exact_document(*, status: str = "PASS"):
    receipt_sha256 = "7" * 64
    return {
        "schema_version": runner.RUN_SCHEMA_VERSION,
        "repeat_count": 2,
        "conclusions_match": True,
        "receipts_match": True,
        "repeated_receipt_sha256": receipt_sha256,
        "status": status,
        "receipts": [
            {"receipt_sha256": receipt_sha256},
            {"receipt_sha256": receipt_sha256},
        ],
    }


def test_repeated_receipts_bind_catalog_denials_and_exact_revision():
    source_revision = "1" * 40

    document = runner.build_audit_run(
        db_url="postgresql://example.invalid/hindsight",
        tenant_id="00000000-0000-0000-0000-000000000002",
        namespace="private-live-namespace",
        source_revision=source_revision,
        repeat=2,
        connect=lambda _url: _AuditConnection(),
        audit_runner=lambda _connection, **_kwargs: _catalog_receipt(source_revision),
        denial_runner=lambda **_kwargs: _denial_receipt(),
    )

    assert document["schema_version"] == runner.RUN_SCHEMA_VERSION
    assert document["status"] == "PASS"
    assert document["conclusions_match"] is True
    assert document["receipts_match"] is True
    assert document["repeated_receipt_sha256"] == document["receipts"][0]["receipt_sha256"]
    assert document["repeat_count"] == 2
    assert document["source_revision"] == source_revision
    assert document["receipts"][0] == document["receipts"][1]
    receipt = document["receipts"][0]
    assert receipt["catalog_conclusion_sha256"] == "c" * 64
    assert receipt["denial_conclusion_sha256"] == "e" * 64
    assert receipt["catalog_receipt"]["official_skill"] == {"commit": "pinned"}
    assert "private-live-namespace" not in json.dumps(document, sort_keys=True)
    runner._require_exact_acceptance(document)  # noqa: SLF001


def test_exact_acceptance_rejects_different_full_receipts_with_same_conclusion():
    source_revision = "1" * 40
    iteration = iter(("4", "5"))

    def audit_runner(_connection, **_kwargs):
        receipt = _catalog_receipt(source_revision)
        receipt["receipt_sha256"] = next(iteration) * 64
        return receipt

    document = runner.build_audit_run(
        db_url="postgresql://example.invalid/hindsight",
        tenant_id="00000000-0000-0000-0000-000000000002",
        namespace="private-live-namespace",
        source_revision=source_revision,
        repeat=2,
        connect=lambda _url: _AuditConnection(),
        audit_runner=audit_runner,
        denial_runner=lambda **_kwargs: _denial_receipt(),
    )

    assert document["conclusions_match"] is True
    assert document["receipts_match"] is False
    assert document["repeated_receipt_sha256"] is None
    assert document["receipts"][0] != document["receipts"][1]
    assert document["status"] == "FAIL"
    with pytest.raises(RuntimeError, match="different full receipts"):
        runner._require_exact_acceptance(document)  # noqa: SLF001


@pytest.mark.parametrize("mismatch", ["auditor", "tenant"])
def test_repeated_receipt_rejects_mismatched_security_scope(mismatch):
    source_revision = "1" * 40
    catalog = _catalog_receipt(source_revision)
    denial = _denial_receipt()
    if mismatch == "auditor":
        denial["auditor_role"] = "unexpected"
    else:
        denial["scope"]["tenant_id_sha256"] = "9" * 64

    with pytest.raises(RuntimeError, match="auditor role|scopes do not match"):
        runner._repeated_receipt(  # noqa: SLF001
            source_revision=source_revision,
            catalog_receipt=catalog,
            denial_receipt=denial,
        )


@pytest.mark.parametrize("status", ["WARN", "FAIL", "UNAVAILABLE"])
def test_exact_acceptance_rejects_every_non_pass_status(status):
    document = _exact_document(status=status)

    with pytest.raises(RuntimeError, match=status):
        runner._require_exact_acceptance(document)  # noqa: SLF001


def test_exact_acceptance_requires_two_runs():
    document = _exact_document()
    document["repeat_count"] = 1
    with pytest.raises(RuntimeError, match="requires two runs"):
        runner._require_exact_acceptance(document)  # noqa: SLF001


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(receipts_match=False),
        lambda document: document.update(repeated_receipt_sha256="8" * 64),
        lambda document: document["receipts"][1].update(receipt_sha256="8" * 64),
        lambda document: document["receipts"][1].update(receipt_sha256="invalid"),
    ],
)
def test_exact_acceptance_recomputes_full_receipt_identity(mutation):
    document = _exact_document()
    mutation(document)

    with pytest.raises(RuntimeError, match="different full receipts"):
        runner._require_exact_acceptance(document)  # noqa: SLF001


def test_main_resolves_scenario_without_serializing_namespace(monkeypatch, tmp_path, capsys):
    output = tmp_path / "audit.json"
    captured = {}
    document = _exact_document()
    monkeypatch.setattr(runner, "database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        runner,
        "resolve_scenario_namespace",
        lambda **_kwargs: "private-live-namespace",
    )

    def build(**kwargs):
        captured.update(kwargs)
        return document

    monkeypatch.setattr(runner, "build_audit_run", build)
    monkeypatch.setattr(
        runner.sys,
        "argv",
        [
            "run_memory_infrastructure_audit.py",
            "--tenant-id",
            "00000000-0000-0000-0000-000000000002",
            "--scenario-id",
            "49109a44-43e7-40de-b547-b4f9d0a387a2",
            "--source-revision",
            "1" * 40,
            "--output",
            str(output),
        ],
    )

    runner.main()

    assert captured["namespace"] == "private-live-namespace"
    assert "private-live-namespace" not in output.read_text()
    assert "private-live-namespace" not in capsys.readouterr().out


def test_main_persists_non_pass_receipt_before_failing(monkeypatch, tmp_path):
    output = tmp_path / "audit.json"
    document = _exact_document(status="UNAVAILABLE")
    monkeypatch.setattr(runner, "database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(runner, "build_audit_run", lambda **_kwargs: document)
    monkeypatch.setattr(
        runner.sys,
        "argv",
        [
            "run_memory_infrastructure_audit.py",
            "--tenant-id",
            "00000000-0000-0000-0000-000000000002",
            "--namespace",
            "private-live-namespace",
            "--source-revision",
            "1" * 40,
            "--output",
            str(output),
        ],
    )

    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        runner.main()

    assert json.loads(output.read_text()) == document
