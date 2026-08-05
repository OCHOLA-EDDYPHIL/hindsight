"""Run explicit stages of the deterministic v5 learning study."""

from __future__ import annotations

import argparse
import json
import pathlib
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
    structural.add_argument("--code-sha", required=True)
    structural.add_argument("--per-family", type=int, default=1_000)
    structural.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()

    if args.command == "protocol":
        _write_json(development_protocol(), output=None)
        return 0
    if args.command == "qualify-structure":
        receipt = qualify_development_structure(
            code_sha=args.code_sha,
            per_family=args.per_family,
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


if __name__ == "__main__":
    raise SystemExit(main())
