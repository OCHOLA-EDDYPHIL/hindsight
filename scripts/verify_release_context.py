"""Fail closed unless a workflow still targets the current exact main release."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


MAIN_REF = "refs/heads/main"
MAX_RESPONSE_BYTES = 1_000_000
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
WORKFLOW_PATH_PATTERN = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        """Refuse redirects before an authorization header can be forwarded."""

        return None


def _open_without_redirects(request: urllib.request.Request, *, timeout: int) -> Any:
    return urllib.request.build_opener(_RejectRedirects()).open(request, timeout=timeout)


def _required_environment(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _require_sha(value: str, *, label: str) -> str:
    if SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase 40-character commit id")
    return value


def _require_https_url(value: str, *, label: str, origin_only: bool) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (origin_only and parsed.path not in {"", "/"})
    ):
        suffix = " origin" if origin_only else " URL"
        raise ValueError(f"{label} must be an HTTPS{suffix} without credentials or parameters")
    return value.rstrip("/")


def verify_runner_context(
    *,
    source_revision: str,
    workflow_path: str,
    monitored_sha: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Validate immutable GitHub identity and exact-main event fields."""

    environment = os.environ if environ is None else environ
    source_revision = _require_sha(source_revision, label="source revision")
    if WORKFLOW_PATH_PATTERN.fullmatch(workflow_path) is None:
        raise ValueError("workflow path must identify one repository workflow file")

    repository = _required_environment(environment, "GITHUB_REPOSITORY")
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("GITHUB_REPOSITORY must be an owner/name pair")
    repository_owner = _required_environment(environment, "GITHUB_REPOSITORY_OWNER")
    if repository.split("/", 1)[0] != repository_owner:
        raise RuntimeError("repository owner does not match the repository identity")

    actor = _required_environment(environment, "GITHUB_ACTOR")
    triggering_actor = _required_environment(environment, "GITHUB_TRIGGERING_ACTOR")
    if actor != repository_owner or triggering_actor != repository_owner:
        raise RuntimeError("both workflow identities must be the repository owner")

    event_name = _required_environment(environment, "GITHUB_EVENT_NAME")
    ref = _required_environment(environment, "GITHUB_REF")
    workflow_ref = _required_environment(environment, "GITHUB_WORKFLOW_REF")
    event_sha = _require_sha(
        _required_environment(environment, "GITHUB_SHA"),
        label="GitHub event SHA",
    )
    expected_workflow_ref = f"{repository}/{workflow_path}@{MAIN_REF}"
    if event_name != "workflow_dispatch":
        raise RuntimeError("release verification requires a workflow_dispatch event")
    if ref != MAIN_REF or workflow_ref != expected_workflow_ref:
        raise RuntimeError("workflow ref is not the exact main workflow")
    if event_sha != source_revision:
        raise RuntimeError("GitHub event SHA does not match the source revision")

    if monitored_sha is not None:
        monitored_sha = _require_sha(monitored_sha, label="monitored SHA")
        if monitored_sha != source_revision:
            raise RuntimeError("monitored SHA does not match the source revision")

    return {
        "repository": repository,
        "repository_owner": repository_owner,
        "source_revision": source_revision,
        "workflow_ref": workflow_ref,
    }


def read_local_head(
    repository_path: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Read the checked-out commit without consulting mutable local refs."""

    result = run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repository_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return _require_sha(result.stdout.strip(), label="local HEAD")


def _read_json(
    request: urllib.request.Request,
    *,
    expected_url: str,
    urlopen: Callable[..., Any],
) -> dict[str, Any]:
    with urlopen(request, timeout=30) as response:
        if getattr(response, "status", None) != 200:
            raise RuntimeError(f"{expected_url} did not return HTTP 200")
        final_url = response.geturl()
        if final_url != expected_url:
            raise RuntimeError(f"{expected_url} redirected to an unexpected URL")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"{expected_url} returned an oversized response")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{expected_url} did not return a JSON object")
    return payload


def fetch_remote_main_sha(
    *,
    api_url: str,
    repository: str,
    token: str,
    urlopen: Callable[..., Any] = _open_without_redirects,
) -> str:
    """Read the current main ref through the authenticated GitHub API."""

    api_url = _require_https_url(api_url, label="GitHub API URL", origin_only=False)
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required")
    repository_path = urllib.parse.quote(repository, safe="/")
    endpoint = f"{api_url}/repos/{repository_path}/git/ref/heads/main"
    api_request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    payload = _read_json(api_request, expected_url=endpoint, urlopen=urlopen)
    target = payload.get("object")
    if payload.get("ref") != MAIN_REF or not isinstance(target, dict):
        raise RuntimeError("GitHub main-ref response has an unexpected identity")
    if target.get("type") != "commit":
        raise RuntimeError("GitHub main ref does not target a commit")
    return _require_sha(str(target.get("sha") or ""), label="remote main SHA")


def verify_deployed_health(
    *,
    deployed_url: str,
    source_revision: str,
    urlopen: Callable[..., Any] = _open_without_redirects,
) -> str:
    """Require an HTTPS deployment to report the exact live revision."""

    origin = _require_https_url(deployed_url, label="deployed URL", origin_only=True)
    endpoint = f"{origin}/v1/health/live"
    health_request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json"},
    )
    payload = _read_json(health_request, expected_url=endpoint, urlopen=urlopen)
    expected = {"status": "live", "revision": source_revision}
    if payload != expected:
        raise RuntimeError("deployed liveness does not match the exact source revision")
    return endpoint


def verify_release_context(
    *,
    source_revision: str,
    workflow_path: str,
    monitored_sha: str | None = None,
    deployed_url: str | None = None,
    repository_path: Path = Path("."),
    environ: Mapping[str, str] | None = None,
    urlopen: Callable[..., Any] = _open_without_redirects,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Verify the runner, checkout, remote main, and optional deployed release."""

    environment = os.environ if environ is None else environ
    context = verify_runner_context(
        source_revision=source_revision,
        workflow_path=workflow_path,
        monitored_sha=monitored_sha,
        environ=environment,
    )
    local_head = read_local_head(repository_path, run=run)
    if local_head != source_revision:
        raise RuntimeError("local HEAD does not match the source revision")
    remote_main = fetch_remote_main_sha(
        api_url=_required_environment(environment, "GITHUB_API_URL"),
        repository=context["repository"],
        token=_required_environment(environment, "GITHUB_TOKEN"),
        urlopen=urlopen,
    )
    if remote_main != source_revision:
        raise RuntimeError("current remote main does not match the source revision")
    if deployed_url is not None:
        verify_deployed_health(
            deployed_url=deployed_url,
            source_revision=source_revision,
            urlopen=urlopen,
        )
    return {
        **context,
        "local_head": local_head,
        "remote_main": remote_main,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--monitored-sha")
    parser.add_argument("--deployed-url")
    parser.add_argument("--repository-path", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        result = verify_release_context(
            source_revision=args.source_revision,
            workflow_path=args.workflow_path,
            monitored_sha=args.monitored_sha,
            deployed_url=args.deployed_url,
            repository_path=args.repository_path,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"release context verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
