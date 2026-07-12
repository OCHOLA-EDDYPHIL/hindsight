"""Build a Lambda deployment zip without committing build artifacts."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build" / "lambda"
PACKAGE_DIR = BUILD_DIR / "package"
ZIP_PATH = BUILD_DIR / "hindsight-agent.zip"


def main() -> None:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    PACKAGE_DIR.mkdir(parents=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(PACKAGE_DIR),
            "--python-version",
            "3.12",
            "--python-platform",
            "x86_64-manylinux2014",
            "--only-binary",
            ":all:",
            "--no-binary",
            "hindsight",
            str(ROOT),
        ],
        check=True,
        cwd=ROOT,
    )
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in PACKAGE_DIR.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(PACKAGE_DIR))
    print(ZIP_PATH)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
