"""Tests for the poison/rewind demo CLI."""

import importlib.util
import pathlib


def _load_cli_module():
    path = pathlib.Path("scripts/run_poison_rewind_demo.py")
    spec = importlib.util.spec_from_file_location("run_poison_rewind_demo", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_poison_rewind_cli_defaults_to_all_command(monkeypatch, capsys):
    cli = _load_cli_module()
    calls = []

    def fake_run_poison_rewind_demo(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli, "run_poison_rewind_demo", fake_run_poison_rewind_demo)
    monkeypatch.setattr("sys.argv", ["run_poison_rewind_demo.py"])

    cli.main()

    assert calls == [
        {
            "namespace": cli.DEMO_NAMESPACE,
            "keep_existing": False,
        }
    ]
    assert '"ok": true' in capsys.readouterr().out
