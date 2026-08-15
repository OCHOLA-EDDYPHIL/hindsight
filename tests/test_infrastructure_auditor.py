"""Pinned Skill and deterministic infrastructure-audit contracts."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

import pytest
from psycopg import sql

import hindsight.infrastructure_auditor as auditor

ROOT = Path(__file__).resolve().parents[1]

SKILL_FIXTURE = b"""\
name: hardening-user-privileges
### 1. Audit Current Users and Roles
### 2. Identify Over-Privileged Users
SHOW GRANTS FOR public
SHOW SYSTEM GRANTS
"""
REFERENCE_FIXTURE = b"""\
# SQL Queries for Privilege Hardening
## User and Role Auditing
## Privilege Auditing
SHOW USERS
SHOW GRANTS ON ROLE admin
"""


def _fixture_fetcher(path: str) -> bytes:
    if path == auditor.OFFICIAL_SKILL_PATH:
        return SKILL_FIXTURE
    if path == auditor.OFFICIAL_REFERENCE_PATH:
        return REFERENCE_FIXTURE
    raise AssertionError(path)


def _configure_fixture_digests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auditor, "OFFICIAL_SKILL_SHA256", sha256(SKILL_FIXTURE).hexdigest())
    monkeypatch.setattr(
        auditor,
        "OFFICIAL_REFERENCE_SHA256",
        sha256(REFERENCE_FIXTURE).hexdigest(),
    )


class _Result:
    def __init__(self, rows=()):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _AuditConnection:
    def __init__(self):
        self.calls: list[object] = []
        self.rows = {
            query.query_id: rows
            for query, rows in zip(
                auditor.AUDIT_CATALOG,
                (
                    [(auditor.AUDITOR_ROLE, "deploy-user", "hindsight")],
                    [(9, 1)],
                    [(0, 38, 0)],
                    [(0,)],
                    [(5, 0, 0, 1, 0, 0, 1)],
                    [(1, 1, 1, 1, 1, True)],
                    [("CockroachDB CCL v25.4.5",)],
                ),
                strict=True,
            )
        }

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        if isinstance(statement, sql.Composed) or statement in {
            "SET TRANSACTION READ ONLY",
            "RESET ROLE",
        } or str(statement).startswith("SELECT set_config"):
            return _Result()
        for query in auditor.AUDIT_CATALOG:
            if statement == query.statement:
                return _Result(self.rows[query.query_id])
        raise AssertionError(statement)


def test_official_skill_material_is_digest_and_marker_verified(monkeypatch):
    _configure_fixture_digests(monkeypatch)

    verified = auditor.verify_official_skill(_fixture_fetcher)

    assert verified["repository"] == "cockroachlabs/cockroachdb-skills"
    assert verified["commit"] == auditor.OFFICIAL_SKILL_COMMIT
    assert verified["skill_git_blob"] == auditor.OFFICIAL_SKILL_GIT_BLOB
    assert verified["reference_git_blob"] == auditor.OFFICIAL_REFERENCE_GIT_BLOB

    with pytest.raises(auditor.SkillVerificationError, match="digest does not match"):
        auditor.verify_official_skill(
            lambda path: b"corrupt" if path == auditor.OFFICIAL_SKILL_PATH else REFERENCE_FIXTURE
        )


def test_pinned_tree_authenticates_the_exact_commit_before_fetching_tree(monkeypatch):
    calls = []

    def github_json(endpoint, *, maximum_bytes):
        calls.append((endpoint, maximum_bytes))
        if endpoint == f"git/commits/{auditor.OFFICIAL_SKILL_COMMIT}":
            return {
                "sha": auditor.OFFICIAL_SKILL_COMMIT,
                "tree": {"sha": auditor.OFFICIAL_SKILL_TREE},
            }
        if endpoint == f"git/trees/{auditor.OFFICIAL_SKILL_TREE}?recursive=1":
            return {
                "sha": auditor.OFFICIAL_SKILL_TREE,
                "truncated": False,
                "tree": [
                    {
                        "path": auditor.OFFICIAL_SKILL_PATH,
                        "sha": auditor.OFFICIAL_SKILL_GIT_BLOB,
                        "type": "blob",
                    },
                    {
                        "path": auditor.OFFICIAL_REFERENCE_PATH,
                        "sha": auditor.OFFICIAL_REFERENCE_GIT_BLOB,
                        "type": "blob",
                    },
                ],
            }
        raise AssertionError(endpoint)

    auditor._pinned_tree.cache_clear()  # noqa: SLF001
    monkeypatch.setattr(auditor, "_github_json", github_json)
    try:
        mapping = auditor._pinned_tree()  # noqa: SLF001
    finally:
        auditor._pinned_tree.cache_clear()  # noqa: SLF001

    assert mapping == {
        auditor.OFFICIAL_SKILL_PATH: auditor.OFFICIAL_SKILL_GIT_BLOB,
        auditor.OFFICIAL_REFERENCE_PATH: auditor.OFFICIAL_REFERENCE_GIT_BLOB,
    }
    assert [endpoint for endpoint, _maximum_bytes in calls] == [
        f"git/commits/{auditor.OFFICIAL_SKILL_COMMIT}",
        f"git/trees/{auditor.OFFICIAL_SKILL_TREE}?recursive=1",
    ]


@pytest.mark.parametrize(
    "commit",
    [
        {"sha": "0" * 40, "tree": {"sha": auditor.OFFICIAL_SKILL_TREE}},
        {"sha": auditor.OFFICIAL_SKILL_COMMIT, "tree": {"sha": "0" * 40}},
    ],
)
def test_pinned_tree_rejects_commit_or_tree_identity_mismatch(monkeypatch, commit):
    calls = []

    def github_json(endpoint, *, maximum_bytes):
        calls.append((endpoint, maximum_bytes))
        return commit

    auditor._pinned_tree.cache_clear()  # noqa: SLF001
    monkeypatch.setattr(auditor, "_github_json", github_json)
    try:
        with pytest.raises(auditor.SkillVerificationError, match="commit.*pinned tree"):
            auditor._pinned_tree()  # noqa: SLF001
    finally:
        auditor._pinned_tree.cache_clear()  # noqa: SLF001

    assert [endpoint for endpoint, _maximum_bytes in calls] == [
        f"git/commits/{auditor.OFFICIAL_SKILL_COMMIT}"
    ]


def test_application_owned_catalog_is_fixed_and_read_only():
    auditor.validate_read_only_catalog()

    assert {query.execution_scope for query in auditor.AUDIT_CATALOG} == {
        "control",
        "auditor",
    }
    for query in auditor.AUDIT_CATALOG:
        assert query.statement.strip().split(maxsplit=1)[0].upper() in {"SELECT", "SHOW", "WITH"}
        assert "crdb_internal." not in query.statement

    with pytest.raises(auditor.InfrastructureAuditError, match="not read-only"):
        auditor.validate_read_only_catalog(
            (
                auditor.AuditQuery(
                    "mutation",
                    "DELETE FROM semantic_memories WHERE false",
                    lambda _rows: ("PASS", "invalid", {}),
                ),
            )
        )


def test_infrastructure_auditor_role_is_nologin_and_exactly_scoped():
    roles = (ROOT / "infra/db/roles.sql").read_text()

    assert "CREATE ROLE IF NOT EXISTS hindsight_infrastructure_auditor NOLOGIN" in roles
    assert "ALTER ROLE hindsight_infrastructure_auditor NOBYPASSRLS" in roles
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public" in roles
    grant = roles.split(
        "-- The infrastructure auditor executes the application-owned read-only", 1
    )[1].split("TO hindsight_infrastructure_auditor;", 1)[0]
    assert "GRANT SELECT ON TABLE" in grant
    assert set(auditor.AUDITOR_TABLES) <= set(re.findall(r"\b[a-z_]+\b", grant))
    assert not any(keyword in grant for keyword in ("INSERT", "UPDATE", "DELETE", "CREATE"))


def test_repeated_audits_have_identical_redacted_conclusions(monkeypatch):
    _configure_fixture_digests(monkeypatch)
    connection = _AuditConnection()
    parameters = {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "namespace": "private-customer-namespace",
        "source_revision": "a" * 40,
        "fetcher": _fixture_fetcher,
    }

    first = auditor.run_infrastructure_audit(connection, **parameters)
    second = auditor.run_infrastructure_audit(_AuditConnection(), **parameters)

    assert first == second
    assert first["status"] == "PASS"
    assert first["conclusion_sha256"] == second["conclusion_sha256"]
    assert first["receipt_sha256"] == second["receipt_sha256"]
    serialized = json.dumps(first)
    assert "private-customer-namespace" not in serialized
    assert "deploy-user" not in serialized
    assert "postgresql://" not in serialized
    version_finding = next(
        finding for finding in first["findings"] if finding["id"] == "cockroach_version"
    )
    assert version_finding["measurements"] == {
        "version_sha256": sha256(b"CockroachDB CCL v25.4.5").hexdigest(),
    }
    assert any(isinstance(statement, sql.Composed) for statement, _params in connection.calls)


def test_missing_tenant_fixture_is_explicitly_unavailable():
    status, code, measurements = auditor._tenant_provenance_evaluator(  # noqa: SLF001
        [(0, 0, 0, 0, 0, True)]
    )

    assert status == "UNAVAILABLE"
    assert code == "tenant_provenance_fixture_unavailable"
    assert measurements["tenant_consistent"] is True
