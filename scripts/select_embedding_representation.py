"""Select and freeze one neutral representation from a complete matrix report."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.representation_selection import select_representation  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    _require_private_path(args.matrix)
    _require_private_path(args.output)
    report = json.loads(args.matrix.read_text(encoding="utf-8"))
    selected = select_representation(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(selected, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    return 0


def _require_private_path(path: pathlib.Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("v4 representation evidence must remain outside the repository")


if __name__ == "__main__":
    raise SystemExit(main())
