"""Fail-closed aggregate verification for the required GitHub Actions status."""

from __future__ import annotations

import json
import os
import sys


ALWAYS_REQUIRED = ("changes", "python_static")
JOB_SELECTIONS = {
    "database": "database",
    "main_qualification": "main_qualification",
    "frontend": "frontend",
    "lambda_artifacts": "lambda_artifacts",
    "terraform": "terraform",
}


def verify(
    *, event_name: str, selections: dict[str, str], results: dict[str, str]
) -> list[str]:
    errors: list[str] = []
    for job in ALWAYS_REQUIRED:
        if results.get(job) != "success":
            errors.append(f"required job {job} ended as {results.get(job, 'missing')}")

    for job, selection in JOB_SELECTIONS.items():
        selected = selections.get(selection)
        result = results.get(job, "missing")
        if selected not in {"true", "false"}:
            errors.append(f"component {selection} has invalid selection {selected!r}")
            continue
        if selected == "true" and result != "success":
            errors.append(f"selected job {job} ended as {result}")
        elif selected == "false" and result not in {"skipped", "success"}:
            errors.append(f"unselected job {job} ended unexpectedly as {result}")
    return errors


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    selections = json.loads(os.environ.get("CI_SELECTIONS", "{}"))
    results = json.loads(os.environ.get("CI_RESULTS", "{}"))
    errors = verify(event_name=event_name, selections=selections, results=results)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
