"""Exact-main normal CI provenance validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_ci_provenance",
    ROOT / "scripts" / "verify_ci_provenance.py",
)
assert SPEC is not None and SPEC.loader is not None
provenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provenance)


def _run(**overrides):
    run = {
        "id": 42,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "path": ".github/workflows/ci.yml",
        "html_url": "https://github.com/owner/project/actions/runs/42",
    }
    return {**run, **overrides}


def test_exact_main_ci_requires_one_completed_success():
    result = provenance.verify_ci_provenance(
        {"total_count": 1, "workflow_runs": [_run()]},
        repository="owner/project",
        sha="a" * 40,
    )

    assert result == {
        "ci_run_id": 42,
        "ci_run_attempt": 1,
        "ci_run_url": "https://github.com/owner/project/actions/runs/42",
        "ci_workflow_path": ".github/workflows/ci.yml",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"total_count": 0, "workflow_runs": []},
        {"total_count": 2, "workflow_runs": [_run(), _run(id=43)]},
        {"total_count": 1, "workflow_runs": [_run(status="in_progress", conclusion=None)]},
        {"total_count": 1, "workflow_runs": [_run(conclusion="failure")]},
        {"total_count": 1, "workflow_runs": [_run(event="pull_request")]},
        {"total_count": 1, "workflow_runs": [_run(head_branch="feature")]},
        {"total_count": 1, "workflow_runs": [_run(head_sha="b" * 40)]},
        {"total_count": 1, "workflow_runs": [_run(path=".github/workflows/other.yml")]},
    ],
)
def test_exact_main_ci_fails_closed(payload):
    with pytest.raises(RuntimeError):
        provenance.verify_ci_provenance(
            payload,
            repository="owner/project",
            sha="a" * 40,
        )
