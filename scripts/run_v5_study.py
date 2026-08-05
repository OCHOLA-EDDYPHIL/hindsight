"""Run explicit stages of the deterministic v5 learning study."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hindsight.v5_corpus import (  # noqa: E402
    development_protocol,
    qualify_development_structure,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("protocol")
    structural = subparsers.add_parser("qualify-structure")
    structural.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()

    if args.command == "protocol":
        _write_json(development_protocol(), output=None)
        return 0
    if args.command == "qualify-structure":
        receipt = qualify_development_structure(
            code_sha=_exact_code_sha(),
        )
        _write_json(receipt, output=args.output)
        return 0
    raise AssertionError(f"unsupported v5 command: {args.command}")


def _write_json(value: object, *, output: pathlib.Path | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")


def _exact_code_sha() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("v5 qualification requires a clean exact-code checkout")
    code_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
        raise RuntimeError("v5 qualification could not resolve an exact code SHA")
    expected = os.environ.get("GITHUB_SHA")
    if expected and expected != code_sha:
        raise RuntimeError("v5 qualification checkout differs from GITHUB_SHA")
    return code_sha


if __name__ == "__main__":
    raise SystemExit(main())
