"""Build dependency-isolated Lambda zip artifacts for the deployed product."""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_PACKAGE = ROOT / "src" / "hindsight"
BUILD_ROOT = ROOT / "build" / "lambda-artifacts"
AWS_LAMBDA_UNZIPPED_LIMIT_BYTES = 262_144_000
# Confirmed from the official AWS ADOT Python layer used by the bounded
# observability deployment. Lambda applies its extracted-size limit to the
# function artifact and every attached layer together.
ADOT_PYTHON_LAYER_UNZIPPED_BYTES = 53_504_816

ARTIFACTS = {
    "api": {
        "dependencies": [
            "certifi>=2026.6.17",
            "cryptography>=50.0.0",
            "fastapi>=0.135.0",
            "google-genai>=2.11.0",
            "mangum>=0.19.0",
            "opentelemetry-api>=1.43.0",
            "opentelemetry-exporter-otlp-proto-grpc>=1.43.0",
            "opentelemetry-sdk>=1.43.0",
            "psycopg[binary]>=3.2",
            "python-dotenv>=1.0",
        ],
        "modules": [
            "__init__.py",
            "api.py",
            "aws.py",
            "db.py",
            "demo_state.py",
            "embedding_index.py",
            "embeddings.py",
            "gemini.py",
            "identity.py",
            "memory.py",
            "operations.py",
            "observability.py",
            "prompt_safety.py",
            "queueing.py",
            "realtime_ticket.py",
            "run_dispatch.py",
            "runs.py",
            "runtime.py",
            "security.py",
            "server_tenants.py",
            "snapshots.py",
            "tenant.py",
            "trace_contract.py",
            "tracing.py",
            "web",
        ],
    },
    "worker": {
        # boto3/botocore are supplied by the managed Python runtime, as they
        # are for the API and realtime artifacts. Bundling a second copy adds
        # more than 20 MB to the extracted worker without changing its API use.
        "dependencies": [
            "certifi>=2026.6.17",
            "cryptography>=50.0.0",
            "google-genai>=2.11.0",
            "langchain-cockroachdb>=0.2.1",
            "langgraph>=1.2.9",
            "opentelemetry-api>=1.43.0",
            "opentelemetry-exporter-otlp-proto-grpc>=1.43.0",
            "opentelemetry-sdk>=1.43.0",
            "psycopg[binary]>=3.2",
            "python-dotenv>=1.0",
        ],
        "modules": [
            "__init__.py",
            "agent.py",
            "agent_decision.py",
            "aws.py",
            "cloudwatch_diagnostics.py",
            "db.py",
            "consolidation.py",
            "embeddings.py",
            "embedding_index.py",
            "gemini.py",
            "memory.py",
            "operations.py",
            "observability.py",
            "prompt_safety.py",
            "queueing.py",
            "reasoning.py",
            "run_dispatch.py",
            "runs.py",
            "runtime.py",
            "security.py",
            "server_tenants.py",
            "tenant.py",
            "tracing.py",
            "worker.py",
        ],
    },
    "realtime": {
        "dependencies": [
            "opentelemetry-api>=1.43.0",
            "opentelemetry-exporter-otlp-proto-grpc>=1.43.0",
            "opentelemetry-sdk>=1.43.0",
        ],
        "modules": [
            "__init__.py",
            "aws.py",
            "observability.py",
            "queueing.py",
            "realtime.py",
            "realtime_ticket.py",
            "security.py",
            "server_tenants.py",
            "tenant.py",
            "tracing.py",
        ],
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*", choices=sorted(ARTIFACTS))
    args = parser.parse_args()
    selected = args.artifacts or list(ARTIFACTS)
    for name in selected:
        print(build_artifact(name))


def build_artifact(name: str) -> pathlib.Path:
    if name not in ARTIFACTS:
        raise ValueError(f"unknown artifact: {name}")
    definition = ARTIFACTS[name]
    artifact_root = BUILD_ROOT / name
    package_root = artifact_root / "package"
    zip_path = BUILD_ROOT / f"hindsight-{name}.zip"
    if artifact_root.exists():
        shutil.rmtree(artifact_root)
    package_root.mkdir(parents=True)
    dependencies = definition["dependencies"]
    if dependencies:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--target",
                str(package_root),
                "--python-version",
                "3.12",
                "--python-platform",
                "x86_64-manylinux2014",
                "--only-binary",
                ":all:",
                *dependencies,
            ],
            cwd=ROOT,
            check=True,
        )
    destination = package_root / "hindsight"
    destination.mkdir()
    for relative in definition["modules"]:
        source = SOURCE_PACKAGE / relative
        target = destination / relative
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in package_root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(package_root))
    validate_unzipped_size(name, zip_path)
    return zip_path


def unzipped_size(zip_path: pathlib.Path) -> int:
    """Return the extracted file bytes Lambda counts for one artifact."""

    with zipfile.ZipFile(zip_path) as archive:
        return sum(member.file_size for member in archive.infolist())


def validate_unzipped_size(name: str, zip_path: pathlib.Path) -> None:
    """Fail before upload when an artifact plus the ADOT layer exceeds Lambda's limit."""

    artifact_bytes = unzipped_size(zip_path)
    combined_bytes = artifact_bytes + ADOT_PYTHON_LAYER_UNZIPPED_BYTES
    if combined_bytes > AWS_LAMBDA_UNZIPPED_LIMIT_BYTES:
        raise RuntimeError(
            f"{name} Lambda artifact and ADOT layer extract to {combined_bytes} bytes; "
            f"limit is {AWS_LAMBDA_UNZIPPED_LIMIT_BYTES} bytes"
        )


if __name__ == "__main__":
    main()
