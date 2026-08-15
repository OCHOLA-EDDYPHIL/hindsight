"""Fail-closed release-context verification tests."""

import importlib.util
import json
import pathlib
import subprocess
import urllib.error

import pytest


SOURCE_REVISION = "a" * 40
WORKFLOW_PATH = ".github/workflows/live-acceptance.yml"


def _module():
    path = pathlib.Path("scripts/verify_release_context.py")
    spec = importlib.util.spec_from_file_location("verify_release_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "GITHUB_ACTOR": "owner",
        "GITHUB_API_URL": "https://api.github.test",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "owner/repository",
        "GITHUB_REPOSITORY_OWNER": "owner",
        "GITHUB_SHA": SOURCE_REVISION,
        "GITHUB_TOKEN": "token",
        "GITHUB_TRIGGERING_ACTOR": "owner",
        "GITHUB_WORKFLOW_REF": (
            "owner/repository/.github/workflows/live-acceptance.yml@refs/heads/main"
        ),
    }
    values.update(overrides)
    return values


class _Response:
    def __init__(self, url: str, payload, *, status: int = 200):
        self.url = url
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, _limit: int):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()


def _git_run(revision: str = SOURCE_REVISION):
    def run(command, **kwargs):
        assert command == ["git", "rev-parse", "--verify", "HEAD"]
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, stdout=f"{revision}\n", stderr="")

    return run


def test_verify_release_context_binds_runner_checkout_remote_and_health():
    verifier = _module()
    requests = []

    def urlopen(request, *, timeout):
        assert timeout == 30
        requests.append(request)
        if request.full_url.endswith("/git/ref/heads/main"):
            return _Response(
                request.full_url,
                {
                    "ref": "refs/heads/main",
                    "object": {"type": "commit", "sha": SOURCE_REVISION},
                },
            )
        return _Response(
            request.full_url,
            {"status": "live", "revision": SOURCE_REVISION},
        )

    result = verifier.verify_release_context(
        source_revision=SOURCE_REVISION,
        workflow_path=WORKFLOW_PATH,
        monitored_sha=SOURCE_REVISION,
        deployed_url="https://demo.example.test/",
        repository_path=pathlib.Path("checkout"),
        environ=_environment(),
        urlopen=urlopen,
        run=_git_run(),
    )

    assert result["local_head"] == SOURCE_REVISION
    assert result["remote_main"] == SOURCE_REVISION
    assert [request.full_url for request in requests] == [
        "https://api.github.test/repos/owner/repository/git/ref/heads/main",
        "https://demo.example.test/v1/health/live",
    ]
    assert requests[0].get_header("Authorization") == "Bearer token"
    assert requests[1].get_header("Authorization") is None


@pytest.mark.parametrize(
    ("environment_change", "error"),
    [
        ({"GITHUB_ACTOR": "maintainer"}, "both workflow identities"),
        ({"GITHUB_TRIGGERING_ACTOR": "maintainer"}, "both workflow identities"),
        ({"GITHUB_REPOSITORY_OWNER": "different"}, "repository owner"),
        ({"GITHUB_EVENT_NAME": "push"}, "workflow_dispatch"),
        ({"GITHUB_REF": "refs/heads/release"}, "exact main workflow"),
        (
            {
                "GITHUB_WORKFLOW_REF": (
                    "owner/repository/.github/workflows/other.yml@refs/heads/main"
                )
            },
            "exact main workflow",
        ),
        ({"GITHUB_SHA": "b" * 40}, "event SHA"),
    ],
)
def test_runner_context_rejects_changed_rerun_identity(environment_change, error):
    verifier = _module()

    with pytest.raises(RuntimeError, match=error):
        verifier.verify_runner_context(
            source_revision=SOURCE_REVISION,
            workflow_path=WORKFLOW_PATH,
            environ=_environment(**environment_change),
        )


@pytest.mark.parametrize("monitored_sha", ["", "B" * 40, "b" * 40])
def test_runner_context_rejects_invalid_or_stale_monitored_sha(monitored_sha):
    verifier = _module()

    with pytest.raises((RuntimeError, ValueError)):
        verifier.verify_runner_context(
            source_revision=SOURCE_REVISION,
            workflow_path=WORKFLOW_PATH,
            monitored_sha=monitored_sha,
            environ=_environment(),
        )


def test_release_context_rejects_wrong_local_head_before_network_access():
    verifier = _module()

    def no_network(*_args, **_kwargs):
        raise AssertionError("network access must not occur for the wrong checkout")

    with pytest.raises(RuntimeError, match="local HEAD"):
        verifier.verify_release_context(
            source_revision=SOURCE_REVISION,
            workflow_path=WORKFLOW_PATH,
            environ=_environment(),
            urlopen=no_network,
            run=_git_run("b" * 40),
        )


def test_release_context_rejects_stale_remote_main():
    verifier = _module()

    def urlopen(request, *, timeout):
        assert timeout == 30
        return _Response(
            request.full_url,
            {
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": "b" * 40},
            },
        )

    with pytest.raises(RuntimeError, match="current remote main"):
        verifier.verify_release_context(
            source_revision=SOURCE_REVISION,
            workflow_path=WORKFLOW_PATH,
            environ=_environment(),
            urlopen=urlopen,
            run=_git_run(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"ref": "refs/heads/release", "object": {"type": "commit", "sha": SOURCE_REVISION}},
        {"ref": "refs/heads/main", "object": {"type": "tag", "sha": SOURCE_REVISION}},
        [],
    ],
)
def test_remote_main_rejects_unexpected_github_payload(payload):
    verifier = _module()
    endpoint = "https://api.github.test/repos/owner/repository/git/ref/heads/main"

    with pytest.raises(RuntimeError):
        verifier.fetch_remote_main_sha(
            api_url="https://api.github.test",
            repository="owner/repository",
            token="token",
            urlopen=lambda *_args, **_kwargs: _Response(endpoint, payload),
        )


def test_remote_main_requires_https_token_and_exact_response_url():
    verifier = _module()

    with pytest.raises(ValueError, match="HTTPS"):
        verifier.fetch_remote_main_sha(
            api_url="http://api.github.test",
            repository="owner/repository",
            token="token",
        )
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        verifier.fetch_remote_main_sha(
            api_url="https://api.github.test",
            repository="owner/repository",
            token="",
        )
    with pytest.raises(RuntimeError, match="redirected"):
        verifier.fetch_remote_main_sha(
            api_url="https://api.github.test",
            repository="owner/repository",
            token="token",
            urlopen=lambda *_args, **_kwargs: _Response(
                "https://attacker.test/ref",
                {
                    "ref": "refs/heads/main",
                    "object": {"type": "commit", "sha": SOURCE_REVISION},
                },
            ),
        )


def test_default_http_handler_rejects_redirects_before_following_them():
    verifier = _module()
    handler = verifier._RejectRedirects()  # noqa: SLF001 - security boundary test

    assert (
        handler.redirect_request(
            object(),
            object(),
            302,
            "Found",
            {},
            "https://attacker.test/ref",
        )
        is None
    )


@pytest.mark.parametrize(
    "deployed_url",
    [
        "http://demo.example.test",
        "https://user@demo.example.test",
        "https://demo.example.test/stage",
        "https://demo.example.test?revision=trusted",
    ],
)
def test_deployed_health_rejects_non_exact_https_origin(deployed_url):
    verifier = _module()

    with pytest.raises(ValueError, match="HTTPS origin"):
        verifier.verify_deployed_health(
            deployed_url=deployed_url,
            source_revision=SOURCE_REVISION,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ready", "revision": SOURCE_REVISION},
        {"status": "live", "revision": "b" * 40},
        {"status": "live", "revision": SOURCE_REVISION, "untrusted": True},
    ],
)
def test_deployed_health_rejects_non_exact_payload(payload):
    verifier = _module()
    endpoint = "https://demo.example.test/v1/health/live"

    with pytest.raises(RuntimeError, match="liveness"):
        verifier.verify_deployed_health(
            deployed_url="https://demo.example.test",
            source_revision=SOURCE_REVISION,
            urlopen=lambda *_args, **_kwargs: _Response(endpoint, payload),
        )


def test_json_reader_fails_closed_on_network_and_oversized_payloads():
    verifier = _module()
    endpoint = "https://demo.example.test/v1/health/live"

    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("unavailable")

    with pytest.raises(urllib.error.URLError):
        verifier.verify_deployed_health(
            deployed_url="https://demo.example.test",
            source_revision=SOURCE_REVISION,
            urlopen=unavailable,
        )
    with pytest.raises(RuntimeError, match="oversized"):
        verifier.verify_deployed_health(
            deployed_url="https://demo.example.test",
            source_revision=SOURCE_REVISION,
            urlopen=lambda *_args, **_kwargs: _Response(
                endpoint,
                b"x" * (verifier.MAX_RESPONSE_BYTES + 1),
            ),
        )
