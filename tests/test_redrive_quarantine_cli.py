"""Protected quarantine redrive command."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _script():
    spec = importlib.util.spec_from_file_location(
        "redrive_quarantine",
        Path("scripts/redrive_quarantine.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arguments():
    quarantine_id = "q_" + "1" * 64
    digest = "2" * 64
    return [
        "--quarantine-id",
        quarantine_id,
        "--digest",
        digest,
        "--confirm",
        f"redrive:{quarantine_id}:{digest}",
    ]


def test_cli_uses_trusted_github_owner_identity(monkeypatch, capsys):
    script = _script()
    table = object()
    calls = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/hindsight")
    monkeypatch.setenv("GITHUB_ACTOR", "owner")
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "owner")
    monkeypatch.setattr(script, "quarantine_table_from_env", lambda: table)
    monkeypatch.setattr(script, "runtime_database_url", lambda: "postgresql://db")
    monkeypatch.setattr(
        script,
        "redrive_quarantined_run",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "quarantine_id": kwargs["quarantine_id"],
                "raw_body_sha256": kwargs["raw_body_sha256"],
                "redrive_effect_id": "33333333-3333-4333-8333-333333333333",
                "run_id": "44444444-4444-4444-8444-444444444444",
                "status": "redriven",
                "created": True,
            }
        ),
    )

    assert script.main(_arguments()) == 0

    assert calls[0]["table"] is table
    assert calls[0]["repository_owner"] == "owner"
    assert calls[0]["actor"] == "owner"
    assert calls[0]["triggering_actor"] == "owner"
    assert calls[0]["db_url"] == "postgresql://db"
    assert '"status": "redriven"' in capsys.readouterr().out


def test_cli_refuses_non_owner_before_aws_or_database_access(monkeypatch, capsys):
    script = _script()
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/hindsight")
    monkeypatch.setenv("GITHUB_ACTOR", "contributor")
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "owner")
    monkeypatch.setattr(
        script,
        "quarantine_table_from_env",
        lambda: (_ for _ in ()).throw(AssertionError("AWS access attempted")),
    )
    monkeypatch.setattr(
        script,
        "runtime_database_url",
        lambda: (_ for _ in ()).throw(AssertionError("database access attempted")),
    )

    assert script.main(_arguments()) == 2
    assert "requires the repository owner" in capsys.readouterr().err
