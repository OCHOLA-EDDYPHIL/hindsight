"""Select the small set of normal CI jobs affected by changed paths."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_test_groups import database_test_files  # noqa: E402


COMPONENTS = (
    "database",
    "main_qualification",
    "frontend",
    "lambda_artifacts",
    "terraform",
)
PRODUCT_TEST_FILES = frozenset(
    Path(path).name for path in database_test_files("product")
)
DATABASE_SCRIPTS = frozenset(
    {
        "apply_database_roles.py",
        "init_db.py",
        "initialize_agent_storage.py",
        "migrate.py",
    }
)
CI_CONTROL_FILES = frozenset(
    {
        "scripts/ci_changes.py",
        "scripts/run_affected_ci.py",
        "scripts/run_migration_compatibility.py",
        "scripts/verify_ci_components.py",
        "tests/test_ci_aggregate.py",
        "tests/test_ci_changes.py",
        "tests/test_ci_contracts.py",
        "tests/test_ci_migration_runner.py",
    }
)


def _none_selected() -> dict[str, bool]:
    return {component: False for component in COMPONENTS}


def classify_paths(paths: Iterable[str], *, event_name: str) -> dict[str, bool]:
    """Return the normal CI jobs affected by the supplied repository paths."""

    selected = _none_selected()
    saw_path = False
    for raw_path in paths:
        path = raw_path.strip().removeprefix("./")
        if not path:
            continue
        saw_path = True
        if path in CI_CONTROL_FILES or path.startswith(".github/workflows/"):
            continue
        if path == "scripts/ci_test_groups.py":
            selected["database"] = True
            continue
        if path.startswith("frontend/") or path in {"package.json", "package-lock.json"}:
            selected["frontend"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("infra/terraform/"):
            selected["terraform"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("src/hindsight/web/"):
            selected["frontend"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("src/hindsight/"):
            selected["database"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("migrations/") or path.startswith("infra/db/"):
            selected["database"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("docs/") or path.endswith((".md", ".rst", ".txt")):
            continue
        if path.startswith("tests/"):
            if path in database_test_files():
                selected["database"] = True
            continue
        if path.startswith("scripts/"):
            name = Path(path).name
            if name in DATABASE_SCRIPTS:
                selected["database"] = True
            elif name in {"build_lambda_artifacts.py", "smoke_lambda_artifacts.py"}:
                selected["lambda_artifacts"] = True
            continue
        if path in {"docker-compose.yml", "pyproject.toml", "uv.lock"}:
            selected["database"] = True
            selected["lambda_artifacts"] = True
            continue
        if path == "Makefile":
            continue

        # Unknown code/configuration gets the product database check, never every tier.
        selected["database"] = True

    if event_name == "push":
        selected["database"] = True
        selected["main_qualification"] = True
        selected["lambda_artifacts"] = True
    elif event_name == "pull_request" and not saw_path:
        selected["database"] = True
    elif event_name not in {"pull_request", "push"}:
        raise ValueError(f"unsupported normal CI event: {event_name}")
    return selected


def changed_paths(*, base_sha: str, head_sha: str) -> list[str]:
    if not base_sha or not head_sha:
        raise ValueError("base and head SHA are required for path classification")
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def write_github_output(path: Path, selected: dict[str, bool]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for component in COMPONENTS:
            output.write(f"{component}={'true' if selected[component] else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    diffable = (
        args.event_name in {"pull_request", "push"}
        and args.base_sha
        and args.head_sha
        and set(args.base_sha) != {"0"}
    )
    paths = (
        changed_paths(base_sha=args.base_sha, head_sha=args.head_sha) if diffable else []
    )
    selected = classify_paths(paths, event_name=args.event_name)
    write_github_output(args.github_output, selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
