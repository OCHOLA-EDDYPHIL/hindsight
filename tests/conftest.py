from __future__ import annotations

import os

import pytest

from hindsight.tenant import tenant_scope

LEGACY_TENANT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def bind_legacy_tenant_for_database_tests(request):
    database_test = any(
        marker.kwargs.get("reason") == "DATABASE_URL not set"
        for marker in request.node.iter_markers("skipif")
    )
    if os.environ.get("DATABASE_URL") and database_test:
        with tenant_scope(LEGACY_TENANT_ID):
            yield
        return
    yield
