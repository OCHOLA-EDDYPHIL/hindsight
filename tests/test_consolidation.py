"""Tests for resolved-incident consolidation."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def test_incident_changefeed_handler_ignores_non_resolved_rows(monkeypatch):
    import hindsight.consolidation as consolidation

    calls = []

    monkeypatch.setattr(
        consolidation,
        "consolidate_resolved_incident",
        lambda **kwargs: calls.append(kwargs),
    )

    results = consolidation.handle_incident_changefeed_event(
        {
            "table": "incidents",
            "value": {
                "after": {
                    "id": str(uuid4()),
                    "slug": "incident-open",
                    "status": "open",
                }
            },
        }
    )

    assert results == []
    assert calls == []


def test_incident_changefeed_handler_consolidates_resolved_after_row(monkeypatch):
    import hindsight.consolidation as consolidation
    from hindsight.consolidation import ConsolidationResult

    incident_id = uuid4()
    calls = []

    def consolidate(**kwargs):
        calls.append(kwargs)
        return ConsolidationResult(
            incident={"id": incident_id, "slug": "incident-resolved"},
            namespace="demo:cross",
            memory={"id": uuid4()},
            created=True,
        )

    monkeypatch.setattr(consolidation, "consolidate_resolved_incident", consolidate)

    results = consolidation.handle_incident_changefeed_event(
        {
            "table": "incidents",
            "value": {
                "after": {
                    "id": str(incident_id),
                    "slug": "incident-resolved",
                    "status": "resolved",
                }
            },
        },
        db_url="postgresql://db",
    )

    assert len(results) == 1
    assert results[0].created is True
    assert calls == [
        {
            "incident_id": str(incident_id),
            "incident_slug": "incident-resolved",
            "db_url": "postgresql://db",
        }
    ]


@requires_db
def test_consolidation_writes_idempotent_lesson_with_provenance():
    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import (
        ROOT_CAUSE,
        open_demo_incident,
        resolve_demo_incident,
    )
    from hindsight.db import database_url

    namespace = f"consolidation-test-{uuid4()}"
    incident = open_demo_incident(
        label="episode-one",
        namespace=namespace,
        summary="checkout latency rose with retry fanout",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )

    first = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
    )
    second = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
    )

    assert first.created is True
    assert first.namespace == namespace
    assert first.memory is not None
    assert first.memory["writer"] == "consolidation.worker"
    assert first.memory["source_ref"] == f"incident:{resolved['slug']}:resolved"
    assert ROOT_CAUSE in first.memory["content"]
    assert second.created is False
    assert second.reason == "lesson already exists"
    assert second.memory is not None
    assert second.memory["id"] == first.memory["id"]
