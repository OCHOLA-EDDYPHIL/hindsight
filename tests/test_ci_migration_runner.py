import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_migration_compatibility",
    ROOT / "scripts/run_migration_compatibility.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_case_databases_are_isolated_and_preserve_connection_options():
    name = runner.case_database_name("qualification_authority", "123456789-2")
    url = runner.database_url(
        "postgresql://root@localhost:26257/defaultdb?sslmode=disable", name
    )

    assert name == "migration_123456789_2_qualification_authority"
    assert url == f"postgresql://root@localhost:26257/{name}?sslmode=disable"


def test_runner_executes_every_case_and_reports_any_failure(monkeypatch):
    created: list[tuple[str, ...]] = []
    executed: list[str] = []

    monkeypatch.setattr(
        runner,
        "create_case_databases",
        lambda _base_url, cases, _run_token: created.append(cases),
    )

    def fake_run(case: str, **_kwargs):
        executed.append(case)
        return case, int(case == "qualification_authority"), f"{case}\n"

    monkeypatch.setattr(runner, "run_case", fake_run)

    assert runner.run_all(base_url="postgresql://unused", run_token="test", workers=2) == 1
    assert created == [tuple(runner.MIGRATION_CASES)]
    assert set(executed) == set(runner.MIGRATION_CASES)
    assert executed.index("agent_runtime_roles") < executed.index("populated_roles")


def test_runner_emits_exact_structured_case_evidence(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runner, "create_case_databases", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "run_case",
        lambda case, **_kwargs: (case, 0, f"{case}\n"),
    )
    output = tmp_path / "migration-history.json"

    assert runner.run_all(
        base_url="postgresql://unused",
        run_token="test",
        workers=2,
        evidence_output=output,
        source_revision="a" * 40,
        workflow_run_id=123,
        workflow_run_attempt=2,
    ) == 0

    document = json.loads(output.read_text())
    assert document == {
        "schema_version": runner.EVIDENCE_SCHEMA,
        "status": "passed",
        "source_revision": "a" * 40,
        "workflow_run": {"id": 123, "attempt": 2},
        "cases": [
            {"name": case, "return_code": 0, "succeeded": True}
            for case in sorted(runner.MIGRATION_CASES)
        ],
    }
