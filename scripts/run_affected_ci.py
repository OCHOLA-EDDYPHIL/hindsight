"""Run the same affected checks selected for a pull request, locally."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_changes import changed_paths, classify_paths  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def timed(name: str, action) -> float:
    started = time.monotonic()
    action()
    elapsed = time.monotonic() - started
    print(f"TIMING {name}={elapsed:.1f}s", flush=True)
    return elapsed


def docker_command() -> list[str]:
    probes = (["docker"], ["sudo", "-n", "docker"])
    for command in probes:
        result = subprocess.run(
            [*command, "info"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return list(command)
    raise RuntimeError("Docker is unavailable (directly and through passwordless sudo)")


def wait_for_database(compose: list[str]) -> None:
    for _ in range(60):
        result = subprocess.run(
            [*compose, "exec", "-T", "crdb", "cockroach", "sql", "--insecure", "-e", "SELECT 1"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("CockroachDB did not become ready within 60 seconds")


def run_static(env: dict[str, str]) -> None:
    run(["uv", "lock", "--check"], env=env)
    run(["uv", "sync", "--frozen"], env=env)
    run(["uv", "run", "python", "scripts/ci_test_groups.py", "validate"], env=env)
    run(["uv", "run", "ruff", "check", "."], env=env)
    run(["uv", "run", "python", "scripts/ci_test_groups.py", "run", "unit"], env=env)


def run_database(env: dict[str, str]) -> None:
    token = re.sub(r"[^a-z0-9]+", "_", f"{os.getpid()}_{int(time.time())}")
    project = f"hindsight_local_ci_{token}"[:63]
    compose = [*docker_command(), "compose", "-p", project]
    database_env = {
        **env,
        "DATABASE_URL": f"postgresql://root@localhost:26257/hindsight_product_{token}?sslmode=disable",
    }
    try:
        run([*compose, "up", "-d", "crdb"], env=env)
        wait_for_database(compose)
        run(
            [
                *compose,
                "exec",
                "-T",
                "crdb",
                "cockroach",
                "sql",
                "--insecure",
                "-e",
                "SET CLUSTER SETTING feature.vector_index.enabled = true",
            ],
            env=env,
        )
        run(["uv", "run", "python", "scripts/migrate.py"], env=database_env)
        run(["uv", "run", "python", "scripts/initialize_agent_storage.py"], env=database_env)
        run(
            ["uv", "run", "python", "scripts/ci_test_groups.py", "run", "product"],
            env=database_env,
        )
    finally:
        run([*compose, "down", "--volumes", "--remove-orphans"], env=env)


def run_frontend(env: dict[str, str]) -> None:
    for command in (
        ["npm", "ci"],
        ["npm", "run", "check:web"],
        ["npm", "run", "test:web"],
        ["npm", "run", "build:web"],
        ["git", "diff", "--exit-code", "--", "src/hindsight/web"],
    ):
        run(command, env=env)


def run_lambda(env: dict[str, str]) -> None:
    run(["uv", "run", "python", "scripts/build_lambda_artifacts.py"], env=env)
    run(["uv", "run", "python", "scripts/smoke_lambda_artifacts.py"], env=env)


def run_terraform(env: dict[str, str]) -> None:
    run(["terraform", "fmt", "-check", "-recursive", "infra/terraform"], env=env)
    for component in ("bootstrap", "app", "edge"):
        directory = f"infra/terraform/{component}"
        run(["terraform", f"-chdir={directory}", "init", "-backend=false", "-input=false"], env=env)
        run(["terraform", f"-chdir={directory}", "validate"], env=env)
        run(["terraform", f"-chdir={directory}", "test"], env=env)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-sha", default="origin/main")
    parser.add_argument("--head-sha", default="HEAD")
    parser.add_argument("--path", action="append", dest="paths")
    parser.add_argument("--list", action="store_true", help="print selection without running checks")
    args = parser.parse_args()

    paths = args.paths or changed_paths(base_sha=args.base_sha, head_sha=args.head_sha)
    selected = classify_paths(paths, event_name="pull_request")
    print(json.dumps({"paths": paths, "selected": selected}, indent=2), flush=True)
    if args.list:
        return 0

    env = {
        **os.environ,
        "EMBEDDING_PROVIDER": "deterministic",
        "LLM_PROVIDER": "deterministic",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEYS"):
        env.pop(name, None)
    timings: dict[str, float] = {}
    timings["python_static"] = timed("python_static", lambda: run_static(env))
    actions = {
        "database": lambda: run_database(env),
        "frontend": lambda: run_frontend(env),
        "lambda_artifacts": lambda: run_lambda(env),
        "terraform": lambda: run_terraform(env),
    }
    for component, action in actions.items():
        if selected[component]:
            timings[component] = timed(component, action)
    print(json.dumps({"selected": selected, "timings_seconds": timings}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
