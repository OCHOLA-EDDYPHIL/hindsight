"""Validate one successful normal CI run for an exact main revision."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def verify_ci_provenance(
    payload: dict[str, Any],
    *,
    repository: str,
    sha: str,
    workflow_path: str = ".github/workflows/ci.yml",
) -> dict[str, Any]:
    """Return normalized run identity or fail closed on any ambiguity."""

    if not repository or "/" not in repository:
        raise ValueError("repository must be an owner/name pair")
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise ValueError("sha must be a lowercase 40-character commit id")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("GitHub Actions response has no workflow_runs list")
    matches = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("path") == workflow_path
    ]
    if payload.get("total_count") != 1 or len(matches) != 1:
        raise RuntimeError("exact-main normal CI run is absent or ambiguous")
    run = matches[0]
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise RuntimeError("exact-main normal CI run is not completed successfully")
    run_id = run.get("id")
    run_attempt = run.get("run_attempt")
    run_url = str(run.get("html_url") or "")
    if not isinstance(run_id, int) or not isinstance(run_attempt, int) or run_attempt < 1:
        raise RuntimeError("exact-main normal CI run identity is incomplete")
    if not run_url.startswith(f"https://github.com/{repository}/actions/runs/"):
        raise RuntimeError("exact-main normal CI run URL does not match the repository")
    return {
        "ci_run_id": run_id,
        "ci_run_attempt": run_attempt,
        "ci_run_url": run_url,
        "ci_workflow_path": workflow_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--workflow-path", default=".github/workflows/ci.yml")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    result = verify_ci_provenance(
        json.load(sys.stdin),
        repository=args.repository,
        sha=args.sha,
        workflow_path=args.workflow_path,
    )
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    else:
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
