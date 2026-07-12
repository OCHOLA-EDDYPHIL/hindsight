"""Tests for Lambda packaging and deployment configuration."""

import pathlib
import subprocess
import importlib.util


def test_lambda_zip_builder_targets_lambda_platform(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "build_lambda_zip",
        pathlib.Path("scripts/build_lambda_zip.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(builder, "BUILD_DIR", tmp_path / "lambda")
    monkeypatch.setattr(builder, "PACKAGE_DIR", tmp_path / "lambda" / "package")
    monkeypatch.setattr(builder, "ZIP_PATH", tmp_path / "lambda" / "hindsight-agent.zip")
    monkeypatch.setattr(subprocess, "run", fake_run)

    builder.main()

    install_args = calls[0][0]
    assert "--python-version" in install_args
    assert install_args[install_args.index("--python-version") + 1] == "3.12"
    assert "--python-platform" in install_args
    assert install_args[install_args.index("--python-platform") + 1] == "x86_64-manylinux2014"
    assert "--only-binary" in install_args
    assert install_args[install_args.index("--only-binary") + 1] == ":all:"
    assert "--no-binary" in install_args
    assert install_args[install_args.index("--no-binary") + 1] == "hindsight"


def test_lambda_template_wires_function_token_and_scopes_bedrock_policy():
    template = pathlib.Path("infra/lambda/template.yaml").read_text()

    assert "FunctionAuthTokenParameterName" in template
    assert "HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM" in template
    assert "parameter${FunctionAuthTokenParameterName}" in template
    assert 'Resource: "*"' not in template
    assert "foundation-model/${BedrockModel}" in template
