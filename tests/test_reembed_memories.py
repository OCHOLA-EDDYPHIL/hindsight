"""Embedding rotation CLI catch-up behavior."""

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest


def _cli():
    path = pathlib.Path("scripts/reembed_memories.py")
    spec = importlib.util.spec_from_file_location("reembed_memories", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_activation_retries_only_after_leasing_catch_up_work(monkeypatch, capsys):
    cli = _cli()
    provider = object()
    results = iter(
        [
            {"leased": 0, "completed": 0, "failed": 0},
            {"leased": 1, "completed": 1, "failed": 0},
            {"leased": 0, "completed": 0, "failed": 0},
        ]
    )
    activation_calls = []
    build_calls = []

    monkeypatch.setattr(
        cli,
        "runtime_settings",
        lambda **_kwargs: SimpleNamespace(
            database_url="postgresql://rotation/database",
            provider_env={"EMBEDDING_PROVIDER": "test"},
        ),
    )
    monkeypatch.setattr(cli, "embedding_provider_from_env", lambda _env: provider)
    monkeypatch.setattr(
        cli,
        "begin_profile_build",
        lambda **kwargs: build_calls.append(kwargs)
        or {"id": "profile-2", "status": "building"},
    )
    monkeypatch.setattr(cli, "run_backfill_batch", lambda **_kwargs: next(results))

    def activate(**_kwargs):
        activation_calls.append(True)
        if len(activation_calls) == 1:
            raise cli.EmbeddingCoverageError("profile coverage incomplete: missing=1, failed=0")
        return {"generation": 2}

    monkeypatch.setattr(cli, "activate_profile", activate)
    monkeypatch.setattr("sys.argv", ["reembed_memories.py"])

    cli.main()

    assert len(activation_calls) == 2
    assert len(build_calls) == 2
    assert all(call["provider"] is provider for call in build_calls)
    assert "embedding profile: processed 1" in capsys.readouterr().out


def test_activation_surfaces_original_coverage_error_without_catch_up_work(monkeypatch):
    cli = _cli()
    provider = object()
    build_calls = []
    monkeypatch.setattr(
        cli,
        "runtime_settings",
        lambda **_kwargs: SimpleNamespace(
            database_url="postgresql://rotation/database",
            provider_env={"EMBEDDING_PROVIDER": "test"},
        ),
    )
    monkeypatch.setattr(cli, "embedding_provider_from_env", lambda _env: provider)
    monkeypatch.setattr(
        cli,
        "begin_profile_build",
        lambda **kwargs: build_calls.append(kwargs)
        or {"id": "profile-2", "status": "building"},
    )
    monkeypatch.setattr(
        cli,
        "run_backfill_batch",
        lambda **_kwargs: {"leased": 0, "completed": 0, "failed": 0},
    )
    monkeypatch.setattr(
        cli,
        "activate_profile",
        lambda **_kwargs: (_ for _ in ()).throw(
            cli.EmbeddingCoverageError("profile coverage incomplete: missing=1, failed=0")
        ),
    )
    monkeypatch.setattr("sys.argv", ["reembed_memories.py"])

    with pytest.raises(cli.EmbeddingCoverageError, match="missing=1"):
        cli.main()
    assert len(build_calls) == 2
