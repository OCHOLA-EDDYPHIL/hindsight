import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER_EXPRESSION = "${{ vars.HINDSIGHT_RUNNER_LABEL }}"
COCKROACH_IMAGE = "cockroachdb/cockroach:v25.4.5"
WORKFLOW_DIRECTORY = ROOT / ".github/workflows"
WORKFLOW_PATHS = tuple(
    sorted(
        (
            *WORKFLOW_DIRECTORY.glob("*.yml"),
            *WORKFLOW_DIRECTORY.glob("*.yaml"),
        )
    )
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
    selector = _load_script("ci_changes")
    verifier = _load_script("verify_ci_components")

    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == set(jobs) - {"test"}
    assert aggregate["steps"][-1]["run"] == "python scripts/verify_ci_components.py"
    assert set(jobs["changes"]["outputs"]) == set(selector.COMPONENTS)
    assert set(verifier.JOB_SELECTIONS) == set(selector.COMPONENTS)
    for component in selector.COMPONENTS:
        assert jobs[component]["if"] == (
            f"needs.changes.outputs.{component} == 'true'"
        )
    assert "migration_compatibility" not in jobs
    assert "research" not in jobs
    migration_runner = _load_script("run_migration_compatibility")
    assert set(migration_runner.PARALLEL_CASES).union(
        migration_runner.ROLE_SENSITIVE_CASES
    ) == {
        "agent_runtime_roles",
        "populated_roles",
        "prompt_safety_upgrade",
        "dispatch_upgrade",
        "qualification_authority",
        "tenant_vector_index",
    }


@pytest.mark.parametrize("workflow_path", WORKFLOW_PATHS, ids=lambda path: path.name)
def test_every_workflow_job_uses_owner_controlled_runner(workflow_path: Path):
    workflow = yaml.safe_load(workflow_path.read_text())
    jobs = list(workflow["jobs"].values())
    executable_jobs = [job for job in jobs if "runs-on" in job]

    assert jobs
    assert all(("runs-on" in job) != ("uses" in job) for job in jobs)
    assert all(job["runs-on"] == RUNNER_EXPRESSION for job in executable_jobs)


def test_reusable_deploy_workflow_uses_owner_controlled_runner():
    workflow = (ROOT / ".github" / "workflows" / "deploy-demo.yml").read_text()

    assert "workflow_call:" in workflow
    assert RUNNER_EXPRESSION in workflow
    assert "ubuntu-latest" not in workflow


def test_normal_ci_and_migration_qualification_pin_supported_cockroach_version():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert f"${{COCKROACH_IMAGE:-{COCKROACH_IMAGE}}}" in compose

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/migration-compatibility.yml").read_text()
    )

    job = workflow["jobs"]["migration_compatibility"]
    assert job["runs-on"] == RUNNER_EXPRESSION
    assert job["env"]["COCKROACH_IMAGE"] == COCKROACH_IMAGE


def test_security_audits_use_writable_cache_and_repo_scoped_exceptions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    python_steps = workflow["jobs"]["python_static"]["steps"]

    assert any(
        step.get("run") == 'uv run pip-audit --cache-dir "$RUNNER_TEMP/pip-audit"'
        for step in python_steps
    )

    terraform_steps = workflow["jobs"]["terraform"]["steps"]
    trivy_step = next(
        step for step in terraform_steps if step.get("uses", "").startswith("aquasecurity/trivy-action@")
    )
    assert "trivyignores" not in trivy_step["with"]
    assert trivy_step["with"]["trivy-config"] == "infra/terraform/trivy.yaml"

    trivy_config = yaml.safe_load((ROOT / "infra/terraform/trivy.yaml").read_text())
    assert trivy_config == {"ignorefile": "infra/terraform/.trivyignore.yaml"}

    ignore = yaml.safe_load((ROOT / "infra/terraform/.trivyignore.yaml").read_text())
    ignored_paths = {
        path
        for entry in ignore["misconfigurations"]
        for path in entry["paths"]
    }
    assert ignored_paths == {
        "app/main.tf",
        "bootstrap/main.tf",
        "lifecycle/main.tf",
    }
    assert {entry["id"] for entry in ignore["misconfigurations"]} == {
        "AWS-0095",
        "AWS-0132",
    }


def test_persistent_runner_databases_are_isolated_by_run_and_attempt():
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = workflow_path.read_text()
    suffix = "_${{ github.run_id }}_${{ github.run_attempt }}?sslmode=disable"
    assert f"hindsight_product{suffix}" in workflow
    assert f"hindsight_fresh{suffix}" in workflow
    assert f"hindsight_populated{suffix}" in workflow
    assert f"hindsight_extended{suffix}" not in workflow

    jobs = yaml.safe_load(workflow)["jobs"]
    compose_scopes = {
        "database": "database",
        "main_qualification": "main",
    }
    for job_name, scope in compose_scopes.items():
        job = jobs[job_name]
        assert job["env"]["COMPOSE_PROJECT_NAME"] == (
            "hindsight_ci_${{ github.run_id }}_${{ github.run_attempt }}_" + scope
        )
        assert job["steps"][-1]["if"] == "always()"
        assert job["steps"][-1]["run"] == (
            "docker compose down --volumes --remove-orphans"
        )
        assert sum(step.get("run") == "docker compose up -d crdb" for step in job["steps"]) == 1


def test_historical_migrations_share_one_manual_container():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/migration-compatibility.yml").read_text()
    )
    job = workflow["jobs"]["migration_compatibility"]

    assert job["timeout-minutes"] == 45
    assert sum(step.get("run") == "docker compose up -d crdb" for step in job["steps"]) == 1
    assert any(
        "scripts/run_migration_compatibility.py" in step.get("run", "")
        for step in job["steps"]
    )
    assert any("--workers 4" in step.get("run", "") for step in job["steps"])
    extended = next(
        step for step in job["steps"] if step.get("name") == "Run extended database safeguards"
    )
    assert "scripts/ci_test_groups.py run main_extended" in extended["run"]
    assert "scripts/migrate.py" in extended["run"]
    assert "matrix" not in job.get("strategy", {})


def test_fast_product_checks_use_exactly_one_server_and_database():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    database_job = workflow["jobs"]["database"]
    group_step = next(
        step
        for step in database_job["steps"]
        if step.get("name") == "Run affected product checks"
    )

    assert sum(
        step.get("run") == "docker compose up -d crdb"
        for step in database_job["steps"]
    ) == 1
    assert "scripts/ci_test_groups.py run product" in group_step["run"]
    assert "main_extended" not in group_step["run"]


def test_main_schema_builds_share_one_server_and_isolate_required_databases():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    main_job = workflow["jobs"]["main_qualification"]
    role_step = next(
        step
        for step in main_job["steps"]
        if step.get("name") == "Establish shared migration roles"
    )
    schema_step = next(
        step
        for step in main_job["steps"]
        if step.get("name") == "Verify populated upgrade and schema parity"
    )

    assert main_job["steps"].index(role_step) < main_job["steps"].index(schema_step)
    assert role_step["run"].count(
        "CREATE ROLE IF NOT EXISTS hindsight_lifecycle NOLOGIN"
    ) == 1
    assert "fresh_pid=$!" in schema_step["run"]
    assert "populated_pid=$!" in schema_step["run"]
    assert 'wait "$fresh_pid"' in schema_step["run"]
    assert 'wait "$populated_pid"' in schema_step["run"]
    assert "main_extended" not in schema_step["run"]
    assert "scripts/schema_manifest.py compare" in schema_step["run"]
    assert all("benchmark" not in step.get("name", "").lower() for step in main_job["steps"])
    assert all("run_learning_benchmark.py" not in step.get("run", "") for step in main_job["steps"])


def test_frozen_research_workflows_and_normal_ci_commands_are_absent():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    migrations = (ROOT / ".github/workflows/migration-compatibility.yml").read_text()

    assert "run_migration_compatibility.py" not in ci
    assert "run_rank_diagnostics.py" not in ci
    assert "run_learning_benchmark.py" not in ci
    assert "workflow_call:" not in migrations
    assert "workflow_dispatch:" in migrations
    for name in (
        "learning-qualification.yml",
        "learning-evidence.yml",
        "v4-corpus-construction.yml",
    ):
        assert not (ROOT / ".github/workflows" / name).exists()


def test_active_workflow_triggers_preserve_automatic_and_manual_boundaries():
    workflow_dir = ROOT / ".github/workflows"
    workflows = {
        path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader)["on"]
        for path in workflow_dir.glob("*.yml")
    }

    assert set(workflows) == {
        "ci.yml",
        "deploy-demo.yml",
        "destroy-demo.yml",
        "live-acceptance.yml",
        "migration-compatibility.yml",
        "observability-evidence.yml",
        "plan-bootstrap.yml",
        "recovery-drill.yml",
        "redrive-quarantine.yml",
        "tenant-lifecycle.yml",
        "verify-deployed.yml",
    }
    assert set(workflows["ci.yml"]) == {"push", "pull_request"}
    assert set(workflows["deploy-demo.yml"]) == {"workflow_call", "workflow_dispatch"}
    for name in (
        "destroy-demo.yml",
        "live-acceptance.yml",
        "migration-compatibility.yml",
        "observability-evidence.yml",
        "plan-bootstrap.yml",
        "recovery-drill.yml",
        "redrive-quarantine.yml",
        "tenant-lifecycle.yml",
    ):
        assert set(workflows[name]) == {"workflow_dispatch"}
    assert set(workflows["verify-deployed.yml"]) == {"schedule", "workflow_dispatch"}
    assert workflows["verify-deployed.yml"]["schedule"] == [
        {"cron": "17 */6 * * *"}
    ]


def test_every_uv_workflow_setup_uses_the_dependency_cache():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                    assert step["with"] == {
                        "enable-cache": True,
                        "cache-dependency-glob": "uv.lock",
                    }, f"{path.name}: {step}"


@pytest.mark.parametrize(
    "workflow_name", ("ci.yml", "deploy-demo.yml", "destroy-demo.yml")
)
def test_terraform_workflows_cache_provider_plugins(workflow_name: str):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text())
    terraform_jobs = [
        job
        for job in workflow["jobs"].values()
        if any(
            step.get("uses", "").startswith("hashicorp/setup-terraform@")
            for step in job.get("steps", [])
        )
    ]

    assert terraform_jobs
    for job in terraform_jobs:
        cache_dir = job.get("env", {}).get(
            "TF_PLUGIN_CACHE_DIR",
            workflow.get("env", {}).get("TF_PLUGIN_CACHE_DIR"),
        )
        assert cache_dir == (
            "${{ github.workspace }}/.terraform.d/plugin-cache"
        )
        assert any(
            step.get("uses", "").startswith("actions/cache@")
            and step.get("with", {}).get("path") == ".terraform.d/plugin-cache"
            for step in job["steps"]
        )
        assert any(
            step.get("run") == 'mkdir -p "$TF_PLUGIN_CACHE_DIR"'
            for step in job["steps"]
        )


def test_local_affected_runner_implements_every_selected_component():
    selector = _load_script("ci_changes")
    runner = _load_script("run_affected_ci")

    assert set(runner.component_actions({})) == set(selector.COMPONENTS)


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


def test_migrate_fences_schema_changes_during_interrupted_purge(
    monkeypatch, tmp_path: Path
):
    migrate = _load_script("migrate")
    (tmp_path / "0030_change.sql").write_text("SELECT 1")

    class Result:
        def __init__(self, rows=()):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=None):
            sql = str(statement)
            if "FROM tenant_lifecycle_operations" in sql:
                return Result(
                    (("00000000-0000-0000-0000-000000000123", "purging"),)
                )
            return Result()

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(migrate, "ensure_database", lambda _url: None)
    monkeypatch.setattr(
        migrate.psycopg, "connect", lambda *_args, **_kwargs: Connection()
    )

    with pytest.raises(
        RuntimeError,
        match="resume or finalize those purge operations before applying schema changes",
    ):
        migrate.apply_migrations("postgresql://unused/db")


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


def test_schema_manifest_normalizes_equivalent_cockroach_view_line_wrapping():
    schema_manifest = _load_script("schema_manifest")
    same_line_alias = """
        CREATE VIEW public.tenant_lifecycle_completeness_issues AS
        WITH tenant_columns AS (
            SELECT columns.table_name
            FROM source_db.information_schema.columns AS columns
            WHERE columns.column_name = 'tenant_id'
        )
        SELECT table_name FROM tenant_columns
    """
    line_broken_alias = """
        CREATE VIEW public.tenant_lifecycle_completeness_issues AS
        WITH tenant_columns AS (
            SELECT columns.table_name
            FROM source_db.information_schema.columns
                AS columns
            WHERE columns.column_name = 'tenant_id'
        )
        SELECT table_name FROM tenant_columns
    """

    assert schema_manifest._normalize(
        same_line_alias,
        database="source_db",
    ) == schema_manifest._normalize(
        line_broken_alias,
        database="source_db",
    )


def test_schema_manifest_view_normalization_keeps_semantic_changes_distinct():
    schema_manifest = _load_script("schema_manifest")
    expected = """
        CREATE VIEW public.tenant_lifecycle_completeness_issues AS
        SELECT table_name FROM source_db.information_schema.columns
        WHERE column_name = 'tenant_id'
    """
    changed_predicate = """
        CREATE VIEW public.tenant_lifecycle_completeness_issues AS
        SELECT table_name FROM source_db.information_schema.columns
        WHERE column_name = 'tenant_key'
    """
    changed_literal_whitespace = expected.replace("'tenant_id'", "'tenant  id'")

    normalized = schema_manifest._normalize(expected, database="source_db")

    assert normalized != schema_manifest._normalize(
        changed_predicate,
        database="source_db",
    )
    assert normalized != schema_manifest._normalize(
        changed_literal_whitespace,
        database="source_db",
    )


def test_terminal_fixture_immutability_check_fails_closed():
    fixture = _load_script("populated_upgrade_fixture")

    class MutableConnection:
        def execute(self, _statement, _params):
            return None

    with pytest.raises(RuntimeError, match="unexpectedly allowed mutation"):
        fixture._expect_immutable(MutableConnection(), "UPDATE fixture", ())
