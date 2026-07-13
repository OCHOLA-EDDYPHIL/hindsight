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

ARTIFACTS = {
    "api": {
        "dependencies": [
            "certifi>=2026.6.17",
            "fastapi>=0.135.0",
            "mangum>=0.19.0",
            "opentelemetry-api>=1.43.0",
            "psycopg[binary]>=3.2",
            "python-dotenv>=1.0",
        ],
        "modules": [
            "__init__.py",
            "api.py",
            "aws.py",
            "dashboard.py",
            "db.py",
            "demo_state.py",
            "embeddings.py",
            "gemini.py",
            "memory.py",
            "operations.py",
            "queueing.py",
            "runs.py",
            "runtime.py",
            "security.py",
            "tracing.py",
            "web",
        ],
    },
    "worker": {
        "dependencies": [
            "boto3>=1.43.46",
            "certifi>=2026.6.17",
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
            "aws.py",
            "db.py",
            "consolidation.py",
            "embeddings.py",
            "gemini.py",
            "memory.py",
            "operations.py",
            "reasoning.py",
            "runs.py",
            "runtime.py",
            "security.py",
            "tracing.py",
            "worker.py",
        ],
    },
    "realtime": {
        "dependencies": [],
        "modules": [
            "__init__.py",
            "aws.py",
            "queueing.py",
            "realtime.py",
            "security.py",
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
    return zip_path


if __name__ == "__main__":
    main()
