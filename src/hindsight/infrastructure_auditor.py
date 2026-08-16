"""Deterministic, redacted infrastructure audit for governed memory."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
import json
import re
from typing import Any, Callable, Literal, Sequence
from urllib import error, request

from psycopg import sql

AUDITOR_ROLE = "hindsight_infrastructure_auditor"
AUDITOR_TABLES = (
    "demo_sessions",
    "agent_runs",
    "memory_reads",
    "semantic_memories",
    "checkpoints",
)
AUDIT_SCHEMA_VERSION = "hindsight.infrastructure-audit.v1"
OFFICIAL_SKILL_REPOSITORY = "cockroachlabs/cockroachdb-skills"
OFFICIAL_SKILL_COMMIT = "e14e86d23ce8ee2e7e40a34ce2944c2502b6eadd"
OFFICIAL_SKILL_PATH = (
    "skills/cockroachdb-security-and-governance/hardening-user-privileges/SKILL.md"
)
OFFICIAL_REFERENCE_PATH = (
    "skills/cockroachdb-security-and-governance/hardening-user-privileges/"
    "references/sql-queries.md"
)
OFFICIAL_SKILL_SHA256 = "9680a57258f3bfa2e7d3125ceaab2ffe7c4fa475d4227df441039180205e8fb7"
OFFICIAL_REFERENCE_SHA256 = (
    "dec79d6fa0d3eb8f443e315876fcd4f858a13d0dd38db5eefd1d9987040ff81a"
)
OFFICIAL_SKILL_GIT_BLOB = "7a9ed2e63c3874a658e9f68bd7c683c2d7f411fb"
OFFICIAL_REFERENCE_GIT_BLOB = "f1b785bf700dde68499717e70b6248401c34bca7"
OFFICIAL_SKILL_TREE = "b180ee1db69e4fdecc06e5cbfdc7eade2d362243"
MAX_SKILL_BYTES = 1_000_000
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
READ_ONLY_PREFIX = re.compile(r"^\s*(?:SELECT|SHOW|WITH)\b", re.IGNORECASE)
MUTATING_SQL = re.compile(
    r"\b(?:ALTER|BACKUP|CREATE|DELETE|DROP|GRANT|INSERT|REVOKE|TRUNCATE|UPDATE|UPSERT)\b",
    re.IGNORECASE,
)
SQL_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")

AuditStatus = Literal["PASS", "WARN", "FAIL", "UNAVAILABLE"]
Fetcher = Callable[[str], bytes]


class SkillVerificationError(RuntimeError):
    """Raised when the pinned official Skill cannot be authenticated."""


class InfrastructureAuditError(RuntimeError):
    """Raised when the deterministic audit contract cannot be completed."""


@dataclass(frozen=True)
class AuditQuery:
    query_id: str
    statement: str
    evaluator: Callable[[Sequence[Sequence[Any]]], tuple[AuditStatus, str, dict[str, Any]]]
    parameters: tuple[str, ...] = ()
    optional: bool = False
    execution_scope: Literal["control", "auditor"] = "auditor"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def _sha256(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return sha256(payload).hexdigest()


def _github_json(endpoint: str, *, maximum_bytes: int) -> dict[str, Any]:
    base_url = f"https://api.github.com/repos/{OFFICIAL_SKILL_REPOSITORY}/{endpoint}"
    envelope: bytes | None = None
    for attempt in range(3):
        delimiter = "&" if "?" in base_url else "?"
        url = f"{base_url}{delimiter}hindsight_audit={attempt + 1}"
        api_request = request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "hindsight-infrastructure-auditor",
            },
        )
        try:
            with request.urlopen(  # noqa: S310 - exact api.github.com HTTPS host
                api_request, timeout=15
            ) as response:
                envelope = response.read(maximum_bytes + 1)
            break
        except error.HTTPError as exc:
            if exc.code not in {404, 422} or attempt == 2:
                raise
    if envelope is None:
        raise SkillVerificationError("official Skill response is unavailable")
    if len(envelope) > maximum_bytes:
        raise SkillVerificationError("official Skill response exceeds the bounded size")
    document = json.loads(envelope)
    if not isinstance(document, dict):
        raise SkillVerificationError("official Skill response is malformed")
    return document


def _fetch_pinned_file(path: str) -> bytes:
    expected_blobs = {
        OFFICIAL_SKILL_PATH: OFFICIAL_SKILL_GIT_BLOB,
        OFFICIAL_REFERENCE_PATH: OFFICIAL_REFERENCE_GIT_BLOB,
    }
    expected_blob = expected_blobs.get(path)
    if expected_blob is None:
        raise SkillVerificationError("official Skill path is not allow-listed")
    if _pinned_tree().get(path) != expected_blob:
        raise SkillVerificationError("official Skill tree path does not match the pinned blob")
    document = _github_json(
        f"git/blobs/{expected_blob}", maximum_bytes=MAX_SKILL_BYTES * 2
    )
    if not isinstance(document, dict) or document.get("encoding") != "base64":
        raise SkillVerificationError("official Skill response is malformed")
    if document.get("sha") != expected_blob:
        raise SkillVerificationError("official Skill response identity does not match")
    encoded = document.get("content")
    if not isinstance(encoded, str):
        raise SkillVerificationError("official Skill response omits content")
    payload = base64.b64decode(encoded, validate=False)
    if len(payload) > MAX_SKILL_BYTES:
        raise SkillVerificationError("official Skill material exceeds the bounded size")
    return payload


@cache
def _pinned_tree() -> dict[str, str]:
    commit = _github_json(
        f"git/commits/{OFFICIAL_SKILL_COMMIT}",
        maximum_bytes=MAX_SKILL_BYTES,
    )
    commit_tree = commit.get("tree")
    if (
        commit.get("sha") != OFFICIAL_SKILL_COMMIT
        or not isinstance(commit_tree, dict)
        or commit_tree.get("sha") != OFFICIAL_SKILL_TREE
    ):
        raise SkillVerificationError("official Skill commit does not match the pinned tree")
    document = _github_json(
        f"git/trees/{OFFICIAL_SKILL_TREE}?recursive=1",
        maximum_bytes=MAX_SKILL_BYTES * 2,
    )
    if (
        document.get("sha") != OFFICIAL_SKILL_TREE
        or document.get("truncated") is not False
        or not isinstance(document.get("tree"), list)
    ):
        raise SkillVerificationError("official Skill tree response is incomplete")
    mapping: dict[str, str] = {}
    for item in document["tree"]:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path, blob = item.get("path"), item.get("sha")
        if isinstance(path, str) and isinstance(blob, str):
            mapping[path] = blob
    return mapping


def verify_official_skill(fetcher: Fetcher = _fetch_pinned_file) -> dict[str, str]:
    """Fetch and authenticate the exact upstream Skill and its SQL reference."""

    expected = (
        (
            "skill",
            OFFICIAL_SKILL_PATH,
            OFFICIAL_SKILL_SHA256,
            OFFICIAL_SKILL_GIT_BLOB,
            (
                b"name: hardening-user-privileges",
                b"### 1. Audit Current Users and Roles",
                b"### 2. Identify Over-Privileged Users",
                b"SHOW GRANTS FOR public",
                b"SHOW SYSTEM GRANTS",
            ),
        ),
        (
            "reference",
            OFFICIAL_REFERENCE_PATH,
            OFFICIAL_REFERENCE_SHA256,
            OFFICIAL_REFERENCE_GIT_BLOB,
            (
                b"# SQL Queries for Privilege Hardening",
                b"## User and Role Auditing",
                b"## Privilege Auditing",
                b"SHOW USERS",
                b"SHOW GRANTS ON ROLE admin",
            ),
        ),
    )
    verified: dict[str, str] = {
        "repository": OFFICIAL_SKILL_REPOSITORY,
        "commit": OFFICIAL_SKILL_COMMIT,
        "commit_tree": OFFICIAL_SKILL_TREE,
        "version": "1.0",
    }
    for label, path, expected_digest, expected_blob, markers in expected:
        try:
            payload = fetcher(path)
        except Exception as exc:
            raise SkillVerificationError(
                f"pinned official {label} could not be fetched"
            ) from exc
        if _sha256(payload) != expected_digest:
            raise SkillVerificationError(f"pinned official {label} digest does not match")
        if any(marker not in payload for marker in markers):
            raise SkillVerificationError(f"pinned official {label} contract markers are absent")
        verified[f"{label}_path"] = path
        verified[f"{label}_sha256"] = expected_digest
        verified[f"{label}_git_blob"] = expected_blob
    return verified


def _single_row(rows: Sequence[Sequence[Any]], query_id: str) -> Sequence[Any]:
    if len(rows) != 1:
        raise InfrastructureAuditError(f"{query_id} did not return exactly one aggregate row")
    return rows[0]


def _identity_evaluator(rows: Sequence[Sequence[Any]]) -> tuple[AuditStatus, str, dict[str, Any]]:
    current_user, session_user, database_name = _single_row(rows, "effective_identity")
    current = str(current_user)
    status: AuditStatus = "PASS" if current == AUDITOR_ROLE else "FAIL"
    return (
        status,
        "effective_auditor_role" if status == "PASS" else "unexpected_effective_role",
        {
            "current_user_matches": current == AUDITOR_ROLE,
            "session_user_sha256": _sha256(str(session_user)),
            "database_sha256": _sha256(str(database_name)),
        },
    )


def _user_inventory_evaluator(
    rows: Sequence[Sequence[Any]],
) -> tuple[AuditStatus, str, dict[str, Any]]:
    user_count, admin_count = _single_row(rows, "user_inventory")
    valid = int(user_count) >= 1 and int(admin_count) >= 1
    return (
        "PASS" if valid else "FAIL",
        "user_and_admin_inventory_available" if valid else "invalid_user_inventory",
        {"user_count": int(user_count), "admin_count": int(admin_count)},
    )


def _public_grants_evaluator(
    rows: Sequence[Sequence[Any]],
) -> tuple[AuditStatus, str, dict[str, Any]]:
    data_grants, routine_execute_grants, other_grants = _single_row(
        rows, "public_data_grants"
    )
    values = {
        "data_grant_count": int(data_grants),
        "routine_execute_grant_count": int(routine_execute_grants),
        "other_non_usage_grant_count": int(other_grants),
    }
    clean = values["data_grant_count"] == 0 and values["other_non_usage_grant_count"] == 0
    return (
        "PASS" if clean else "WARN",
        "public_data_grants_absent" if clean else "public_data_grants_require_review",
        values,
    )


def _system_grants_evaluator(
    rows: Sequence[Sequence[Any]],
) -> tuple[AuditStatus, str, dict[str, Any]]:
    (grant_count,) = _single_row(rows, "sensitive_system_grants")
    count = int(grant_count)
    return (
        "PASS" if count == 0 else "WARN",
        "sensitive_system_grants_absent"
        if count == 0
        else "sensitive_system_grants_require_review",
        {"sensitive_system_grant_count": count},
    )


def _auditor_grants_evaluator(
    rows: Sequence[Sequence[Any]],
) -> tuple[AuditStatus, str, dict[str, Any]]:
    (
        table_selects,
        mutating_grants,
        grantable_grants,
        schema_usage,
        schema_create,
        inherited_roles,
        restricted_role,
    ) = _single_row(rows, "auditor_grants")
    values = {
        "table_select_count": int(table_selects),
        "mutating_grant_count": int(mutating_grants),
        "grantable_grant_count": int(grantable_grants),
        "schema_usage_count": int(schema_usage),
        "schema_create_count": int(schema_create),
        "inherited_role_count": int(inherited_roles),
        "restricted_role_count": int(restricted_role),
    }
    valid = (
        values["table_select_count"] == len(AUDITOR_TABLES)
        and values["mutating_grant_count"] == 0
        and values["grantable_grant_count"] == 0
        and values["schema_usage_count"] == 1
        and values["schema_create_count"] == 0
        and values["inherited_role_count"] == 0
        and values["restricted_role_count"] == 1
    )
    return (
        "PASS" if valid else "FAIL",
        "auditor_is_select_only" if valid else "auditor_has_excessive_or_missing_grants",
        values,
    )


def _tenant_provenance_evaluator(
    rows: Sequence[Sequence[Any]],
) -> tuple[AuditStatus, str, dict[str, Any]]:
    session_count, run_count, read_count, memory_count, checkpoint_count, tenant_consistent = (
        _single_row(rows, "tenant_causal_provenance")
    )
    values = {
        "session_count": int(session_count),
        "run_count": int(run_count),
        "read_count": int(read_count),
        "cited_memory_count": int(memory_count),
        "checkpoint_count": int(checkpoint_count),
        "tenant_consistent": bool(tenant_consistent),
    }
    if not values["tenant_consistent"]:
        return "FAIL", "tenant_provenance_inconsistent", values
    if values["session_count"] == 0 or values["run_count"] == 0:
        return "UNAVAILABLE", "tenant_provenance_fixture_unavailable", values
    return "PASS", "tenant_provenance_consistent", values


def _cockroach_version_evaluator(
    rows: Sequence[Sequence[Any]],
) -> tuple[AuditStatus, str, dict[str, Any]]:
    (version,) = _single_row(rows, "cockroach_version")
    version_text = str(version)
    valid = "cockroach" in version_text.lower()
    return (
        "PASS" if valid else "FAIL",
        "cockroach_version_identified" if valid else "cockroach_version_invalid",
        {"version_sha256": _sha256(version_text)},
    )


AUDIT_CATALOG: tuple[AuditQuery, ...] = (
    AuditQuery(
        "effective_identity",
        "SELECT current_user::STRING, session_user::STRING, current_database()::STRING",
        _identity_evaluator,
    ),
    AuditQuery(
        "user_inventory",
        """
        SELECT
            (SELECT count(*)::INT8 FROM [SHOW USERS]),
            (SELECT count(*)::INT8 FROM [SHOW GRANTS ON ROLE admin])
        """,
        _user_inventory_evaluator,
        execution_scope="control",
    ),
    AuditQuery(
        "public_data_grants",
        """
        SELECT
            count(*) FILTER (
                WHERE privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
            )::INT8,
            count(*) FILTER (
                WHERE object_type = 'routine' AND privilege_type = 'EXECUTE'
            )::INT8,
            count(*) FILTER (
                WHERE privilege_type NOT IN ('USAGE', 'EXECUTE', 'SELECT',
                                             'INSERT', 'UPDATE', 'DELETE')
            )::INT8
        FROM [SHOW GRANTS FOR public]
        WHERE schema_name = 'public'
        """,
        _public_grants_evaluator,
        execution_scope="control",
    ),
    AuditQuery(
        "sensitive_system_grants",
        """
        SELECT count(*)::INT8
        FROM [SHOW SYSTEM GRANTS]
        WHERE privilege_type IN (
            'MODIFYCLUSTERSETTING', 'CANCELQUERY', 'CANCELSESSION',
            'VIEWACTIVITY', 'CREATEDB', 'CREATELOGIN'
        )
        """,
        _system_grants_evaluator,
        execution_scope="control",
    ),
    AuditQuery(
        "auditor_grants",
        """
        SELECT
            count(*) FILTER (WHERE privilege_type = 'SELECT')::INT8,
            count(*) FILTER (
                WHERE privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP')
            )::INT8,
            count(*) FILTER (WHERE is_grantable = 'YES')::INT8,
            (SELECT count(*)::INT8
             FROM information_schema.schema_privileges
             WHERE grantee = 'hindsight_infrastructure_auditor'
               AND table_schema = 'public' AND privilege_type = 'USAGE'),
            (SELECT count(*)::INT8
             FROM information_schema.schema_privileges
             WHERE grantee = 'hindsight_infrastructure_auditor'
               AND table_schema = 'public' AND privilege_type = 'CREATE'),
            (SELECT coalesce(array_length(member_of, 1), 0)::INT8
             FROM [SHOW USERS]
             WHERE username = 'hindsight_infrastructure_auditor'),
            (SELECT count(*)::INT8
             FROM pg_roles
             WHERE rolname = 'hindsight_infrastructure_auditor'
               AND NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls)
        FROM information_schema.table_privileges
        WHERE grantee = 'hindsight_infrastructure_auditor'
          AND table_schema = 'public'
        """,
        _auditor_grants_evaluator,
        execution_scope="control",
    ),
    AuditQuery(
        "tenant_causal_provenance",
        """
        SELECT
            count(DISTINCT session.id)::INT8,
            count(DISTINCT run.id)::INT8,
            count(DISTINCT read.id)::INT8,
            count(DISTINCT memory.id)::INT8,
            count(DISTINCT checkpoint.checkpoint_id)::INT8,
            coalesce(bool_and(
                (run.id IS NULL OR run.tenant_id = session.tenant_id)
                AND (read.id IS NULL OR read.tenant_id = session.tenant_id)
                AND (memory.id IS NULL OR memory.tenant_id = session.tenant_id)
                AND (checkpoint.checkpoint_id IS NULL
                     OR checkpoint.tenant_id = session.tenant_id)
            ), true)
        FROM demo_sessions AS session
        LEFT JOIN agent_runs AS run
          ON run.tenant_id = session.tenant_id AND run.namespace = session.namespace
        LEFT JOIN memory_reads AS read
          ON read.tenant_id = run.tenant_id AND read.decision_id = run.decision_id
        LEFT JOIN semantic_memories AS memory
          ON memory.tenant_id = read.tenant_id
         AND read.memory_kind = 'semantic' AND memory.id = read.memory_id
        LEFT JOIN checkpoints AS checkpoint
          ON checkpoint.tenant_id = run.tenant_id AND checkpoint.thread_id = run.thread_id
        WHERE session.tenant_id = current_hindsight_tenant_id()
          AND session.namespace = %s
        """,
        _tenant_provenance_evaluator,
        parameters=("namespace",),
    ),
    AuditQuery(
        "cockroach_version",
        "SELECT version()::STRING",
        _cockroach_version_evaluator,
    ),
)


def validate_read_only_catalog(catalog: Sequence[AuditQuery] = AUDIT_CATALOG) -> None:
    """Reject catalog drift into mutation or multiple-statement execution."""

    identifiers: set[str] = set()
    for query in catalog:
        if query.query_id in identifiers:
            raise InfrastructureAuditError(f"duplicate audit query id: {query.query_id}")
        identifiers.add(query.query_id)
        statement = query.statement.strip()
        statement_without_literals = SQL_STRING_LITERAL.sub("''", statement)
        if not READ_ONLY_PREFIX.match(statement) or MUTATING_SQL.search(
            statement_without_literals
        ):
            raise InfrastructureAuditError(f"audit query is not read-only: {query.query_id}")
        if ";" in statement:
            raise InfrastructureAuditError(f"audit query contains a statement separator: {query.query_id}")
        if any(parameter != "namespace" for parameter in query.parameters):
            raise InfrastructureAuditError(f"audit query has an unknown parameter: {query.query_id}")


def _overall_status(findings: Sequence[dict[str, Any]]) -> AuditStatus:
    statuses = {str(finding["status"]) for finding in findings}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    if "UNAVAILABLE" in statuses:
        return "UNAVAILABLE"
    return "PASS"


def run_infrastructure_audit(
    conn: Any,
    *,
    tenant_id: str,
    namespace: str,
    source_revision: str,
    fetcher: Fetcher = _fetch_pinned_file,
    assume_role: bool = True,
) -> dict[str, Any]:
    """Run the authenticated catalog and return a redacted receipt."""

    if SHA_PATTERN.fullmatch(source_revision) is None:
        raise InfrastructureAuditError("source revision must be an exact lowercase commit SHA")
    if not tenant_id or not namespace:
        raise InfrastructureAuditError("tenant id and namespace are required")
    validate_read_only_catalog()
    skill = verify_official_skill(fetcher)

    conn.execute("SET TRANSACTION READ ONLY")
    conn.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))

    findings: list[dict[str, Any]] = []
    execution_scope: Literal["control", "auditor"] = "control"
    for query in AUDIT_CATALOG:
        if assume_role and query.execution_scope != execution_scope:
            if query.execution_scope == "auditor":
                conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(AUDITOR_ROLE)))
            else:
                conn.execute("RESET ROLE")
            execution_scope = query.execution_scope
        params = tuple(namespace for parameter in query.parameters if parameter == "namespace")
        query_digest = _sha256(query.statement.strip())
        try:
            rows = conn.execute(query.statement, params or None).fetchall()
            status, code, measurements = query.evaluator(rows)
            result_digest = _sha256(_canonical_bytes(rows))
        except Exception:
            if not query.optional:
                raise InfrastructureAuditError(f"required audit query failed: {query.query_id}") from None
            status, code, measurements = "UNAVAILABLE", "optional_query_unavailable", {}
            result_digest = _sha256(b"unavailable")
        findings.append(
            {
                "id": query.query_id,
                "status": status,
                "code": code,
                "query_sha256": query_digest,
                "result_sha256": result_digest,
                "execution_scope": query.execution_scope,
                "measurements": measurements,
            }
        )

    deterministic = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "source_revision": source_revision,
        "auditor_role": AUDITOR_ROLE,
        "official_skill": skill,
        "scope": {
            "tenant_id_sha256": _sha256(tenant_id),
            "namespace_sha256": _sha256(namespace),
        },
        "tool_policy": {
            "catalog_mode": "fixed-read-only",
            "allowed_prefixes": ["SELECT", "SHOW", "WITH"],
            "official_skill_steps": [1, 2],
            "control_scope": "deployment-identity-read-only-transaction",
            "auditor_scope": "dedicated-role-rls-curated-selects",
            "managed_mcp": "supplementary-not-used-for-identity",
        },
        "findings": findings,
        "status": _overall_status(findings),
    }
    conclusion_payload = {
        "schema_version": deterministic["schema_version"],
        "source_revision": deterministic["source_revision"],
        "official_skill": deterministic["official_skill"],
        "scope": deterministic["scope"],
        "findings": [
            {
                "id": finding["id"],
                "status": finding["status"],
                "code": finding["code"],
                "query_sha256": finding["query_sha256"],
                "execution_scope": finding["execution_scope"],
            }
            for finding in findings
        ],
        "status": deterministic["status"],
    }
    deterministic["conclusion_sha256"] = _sha256(_canonical_bytes(conclusion_payload))
    deterministic["receipt_sha256"] = _sha256(_canonical_bytes(deterministic))
    return deterministic
