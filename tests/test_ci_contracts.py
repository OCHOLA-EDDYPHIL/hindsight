import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER_EXPRESSION = "${{ vars.HINDSIGHT_RUNNER_LABEL || 'ubuntu-latest' }}"
RUNNER_ROUTED_WORKFLOWS = (
    "ci.yml",
    "deploy-demo.yml",
    "live-acceptance.yml",
    "learning-qualification.yml",
    "learning-evidence.yml",
    "v4-corpus-construction.yml",
)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_test_inventory_is_complete_and_migration_cases_are_isolated():
    groups = _load_script("ci_test_groups")

    assert groups.inventory_errors() == []
    assert set(groups.MIGRATION_CASES.values()) == groups._decorated_tests(
        "pytest.mark.migration_acceptance"
    )
    for database_group in groups.DATABASE_GROUPS:
        assert groups.pytest_args(database_group)[-2:] == [
            "-m",
            "not migration_acceptance",
        ]


def test_ci_workflow_has_one_fail_closed_aggregate_over_every_component():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    aggregate = jobs["test"]

    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == set(jobs) - {"test"}
    assert aggregate["steps"][-1]["run"] == "python scripts/verify_ci_components.py"
    assert set(jobs["migration_replay"]["strategy"]["matrix"]["case"]) == {
        "benchmark_upgrade",
        "benchmark_fresh",
        "benchmark_preparation",
        "benchmark_finalizer",
        "benchmark_retry",
        "agent_runtime_roles",
        "populated_roles",
        "dispatch_upgrade",
        "qualification_authority",
    }


@pytest.mark.parametrize("workflow_name", RUNNER_ROUTED_WORKFLOWS)
def test_hosted_workflow_jobs_honor_opt_in_runner_override(workflow_name: str):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / workflow_name).read_text())
    jobs = workflow["jobs"].values()
    executable_jobs = [job for job in jobs if "runs-on" in job]

    assert executable_jobs
    assert all("runs-on" in job or "uses" in job for job in jobs)
    assert {job["runs-on"] for job in executable_jobs} == {RUNNER_EXPRESSION}


def test_reusable_deploy_workflow_preserves_hosted_runner_default():
    workflow = (ROOT / ".github" / "workflows" / "deploy-demo.yml").read_text()

    assert "workflow_call:" in workflow
    assert RUNNER_EXPRESSION in workflow
    assert "runs-on: ubuntu-latest" not in workflow


def test_persistent_runner_databases_are_isolated_by_run_and_attempt():
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = workflow_path.read_text()
    suffix = "_${{ github.run_id }}_${{ github.run_attempt }}?sslmode=disable"

    assert f"${{{{ matrix.database }}}}{suffix}" in workflow
    for database_name in (
        "hindsight_diagnostic_ci",
        "hindsight_schema_fresh",
        "hindsight_schema_populated",
    ):
        assert f"{database_name}{suffix}" in workflow

    jobs = yaml.safe_load(workflow)["jobs"]
    compose_scopes = {
        "database": "${{ matrix.group }}",
        "migration_replay": "${{ matrix.case }}",
        "diagnostics": "diagnostics",
        "schema_fresh": "schema_fresh",
        "schema_populated": "schema_populated",
    }
    for job_name, scope in compose_scopes.items():
        job = jobs[job_name]
        assert job["env"]["COMPOSE_PROJECT_NAME"] == (
            "hindsight_ci_${{ github.run_id }}_${{ github.run_attempt }}_" + scope
        )
        assert job["steps"][-1] == {
            "name": "Remove isolated database container",
            "if": "always()",
            "run": "docker compose down --volumes --remove-orphans",
        }


def test_migrate_through_applies_only_the_requested_prefix(monkeypatch, tmp_path: Path):
    migrate = _load_script("migrate")
    for name in ("0001_one.sql", "0002_two.sql", "0003_three.sql"):
        (tmp_path / name).write_text(f"SELECT '{name}'")

    class Result:
        def fetchall(self):
            return []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))
            return Result()

        def transaction(self):
            return Transaction()

    connection = Connection()
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(migrate, "ensure_database", lambda _url: None)
    monkeypatch.setattr(migrate.psycopg, "connect", lambda *_args, **_kwargs: connection)

    assert migrate.apply_migrations("postgresql://unused/db", through="0002_two.sql") == 2
    inserted = [params[0] for statement, params in connection.statements if params]
    assert inserted == ["0001_one.sql", "0002_two.sql"]

    with pytest.raises(ValueError, match="unknown migration filename"):
        migrate.apply_migrations("postgresql://unused/db", through="missing.sql")


def test_schema_manifest_compare_reports_differing_sections(tmp_path: Path):
    schema_manifest = _load_script("schema_manifest")
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text(json.dumps({"tables": ["a"], "roles": []}))
    right.write_text(json.dumps({"tables": ["b"], "roles": []}))

    with pytest.raises(RuntimeError, match="schema manifests differ in: tables"):
        schema_manifest.compare(left, right)

    right.write_text(left.read_text())
    schema_manifest.compare(left, right)


def test_schema_manifest_normalizes_database_names_in_nested_sections():
    schema_manifest = _load_script("schema_manifest")

    normalized = schema_manifest._normalize(
        {
            "tables": ["CREATE VIEW example AS SELECT * FROM source_db.public.rows"],
            "triggers": [["source_db.public.guard()"]],
        },
        database="source_db",
    )

    assert normalized == {
        "tables": ["CREATE VIEW example AS SELECT * FROM <database>.public.rows"],
        "triggers": [["<database>.public.guard()"]],
    }


def test_terminal_fixture_immutability_check_fails_closed():
    fixture = _load_script("populated_upgrade_fixture")

    class MutableConnection:
        def execute(self, _statement, _params):
            return None

    with pytest.raises(RuntimeError, match="unexpectedly allowed mutation"):
        fixture._expect_immutable(MutableConnection(), "UPDATE fixture", ())
