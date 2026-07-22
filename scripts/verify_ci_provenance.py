"""Validate one successful normal CI run for an exact main revision."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
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


def fetch_ci_runs(*, api_url: str, repository: str, sha: str, token: str) -> dict[str, Any]:
    """Fetch normal CI runs without requiring a runner-installed GitHub client."""

    if not api_url.startswith("https://"):
        raise ValueError("GitHub API URL must use HTTPS")
    if not repository or "/" not in repository:
        raise ValueError("repository must be an owner/name pair")
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise ValueError("sha must be a lowercase 40-character commit id")
    if not token:
        raise ValueError("GitHub token is required")
    query = urllib.parse.urlencode(
        {
            "branch": "main",
            "event": "push",
            "head_sha": sha,
            "per_page": 100,
        }
    )
    repository_path = urllib.parse.quote(repository, safe="/")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/repos/{repository_path}/actions/workflows/ci.yml/runs?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("GitHub Actions response is not an object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--workflow-path", default=".github/workflows/ci.yml")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    payload = (
        fetch_ci_runs(
            api_url=os.environ.get("GITHUB_API_URL", ""),
            repository=args.repository,
            sha=args.sha,
            token=os.environ.get("GITHUB_TOKEN", ""),
        )
        if args.fetch
        else json.load(sys.stdin)
    )
    result = verify_ci_provenance(
        payload,
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
