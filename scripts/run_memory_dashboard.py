"""Run the Hindsight live memory dashboard."""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.dashboard import run_dashboard_server  # noqa: E402
from hindsight.demo import DEMO_NAMESPACE  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--namespace", default=DEMO_NAMESPACE)
    args = parser.parse_args()

    run_dashboard_server(host=args.host, port=args.port, namespace=args.namespace)


if __name__ == "__main__":
    main()
