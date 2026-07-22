"""Exact-main normal CI provenance validation."""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
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


def test_fetches_exact_main_runs_with_authenticated_filtered_query(monkeypatch):
    sha = "a" * 40
    response_payload = {"total_count": 0, "workflow_runs": []}
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(response_payload).encode()

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(provenance.urllib.request, "urlopen", urlopen)

    result = provenance.fetch_ci_runs(
        api_url="https://api.github.example",
        repository="owner/project",
        sha=sha,
        token="secret-token",
    )

    assert result == response_payload
    assert captured["timeout"] == 30
    request = captured["request"]
    parsed = urllib.parse.urlsplit(request.full_url)
    assert parsed.path == "/repos/owner/project/actions/workflows/ci.yml/runs"
    assert urllib.parse.parse_qs(parsed.query) == {
        "branch": ["main"],
        "event": ["push"],
        "head_sha": [sha],
        "per_page": ["100"],
    }
    assert request.headers["Authorization"] == "Bearer secret-token"
