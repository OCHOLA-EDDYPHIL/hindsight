"""Tenant lifecycle primitives that do not require a running database."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from hindsight.lifecycle import (
    LifecycleColumn,
    LifecycleConflictError,
    LifecycleTable,
    assert_purgeable_tenant,
    canonical_json_bytes,
    manifest_fingerprint,
    schema_identity_sha256,
    tenant_identity_sha256,
)
from hindsight.server_tenants import (
    ACCEPTANCE_TENANT_ID,
    LEARNING_TENANT_ID,
    PUBLIC_DEMO_TENANT_ID,
)
from hindsight.tenant import tenant_lifecycle_fence_key


def test_canonical_export_json_is_lossless_and_stable():
    value = {
        "binary": b"\x00\xff",
        "decimal": Decimal("1.2300"),
        "float": -0.0,
        "timestamp": datetime(2026, 8, 9, 12, 30, 1, 42, tzinfo=timezone.utc),
        "uuid": UUID("11111111-1111-1111-1111-111111111111"),
    }

    encoded = canonical_json_bytes(value)

    assert encoded == (
        b'{"binary":{"$base64":"AP8="},"decimal":{"$decimal":"1.2300"},'
        b'"float":{"$float":"-0x0.0p+0"},'
        b'"timestamp":{"$timestamp":"2026-08-09T12:30:01.000042+00:00"},'
        b'"uuid":{"$uuid":"11111111-1111-1111-1111-111111111111"}}'
    )
    assert manifest_fingerprint(value) == manifest_fingerprint(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN")])
def test_canonical_export_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes(value)


def test_tenant_hash_is_domain_separated_and_normalized():
    tenant_id = "11111111-1111-1111-1111-111111111111"

    assert tenant_identity_sha256(UUID(tenant_id)) == tenant_identity_sha256(tenant_id)
    assert tenant_identity_sha256(tenant_id) != __import__("hashlib").sha256(
        tenant_id.encode()
    ).hexdigest()

    fence_key = tenant_lifecycle_fence_key(tenant_id)
    assert tenant_id not in fence_key
    assert fence_key == tenant_lifecycle_fence_key(UUID(tenant_id))
    assert fence_key != f"tenant-lifecycle-fence:{tenant_identity_sha256(tenant_id)}"


@pytest.mark.parametrize(
    "tenant_id", [PUBLIC_DEMO_TENANT_ID, ACCEPTANCE_TENANT_ID, LEARNING_TENANT_ID]
)
def test_server_owned_tenants_cannot_enter_purge(tenant_id):
    with pytest.raises(LifecycleConflictError, match="server-owned"):
        assert_purgeable_tenant(tenant_id)


def test_schema_identity_includes_order_columns_keys_and_classification():
    base = LifecycleTable(
        table_name="incidents",
        table_class="tenant_owned",
        tenant_column="tenant_id",
        export_order=3,
        primary_key=("tenant_id", "id"),
        columns=("tenant_id", "id", "status"),
    )
    changed = LifecycleTable(
        **{**base.__dict__, "columns": (*base.columns, "summary")}
    )

    assert schema_identity_sha256((base,)) != schema_identity_sha256((changed,))


def test_schema_identity_includes_column_type_and_nullability():
    definition = LifecycleColumn(
        name="summary",
        data_type="character varying",
        udt_name="varchar",
        crdb_sql_type="VARCHAR(256)",
        nullable=False,
        character_maximum_length=256,
        numeric_precision=None,
        numeric_scale=None,
        datetime_precision=None,
    )
    base = LifecycleTable(
        table_name="incidents",
        table_class="tenant_owned",
        tenant_column="tenant_id",
        export_order=3,
        primary_key=("tenant_id", "id"),
        columns=("summary",),
        column_definitions=(definition,),
    )
    nullable = LifecycleTable(
        **{
            **base.__dict__,
            "column_definitions": (
                LifecycleColumn(**{**definition.__dict__, "nullable": True}),
            ),
        }
    )

    assert schema_identity_sha256((base,)) != schema_identity_sha256((nullable,))
