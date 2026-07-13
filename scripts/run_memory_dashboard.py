"""Run the Hindsight live memory dashboard."""

from __future__ import annotations

import argparse
import os
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
    parser.add_argument("--db-url", default=None)
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("HINDSIGHT_DASHBOARD_AUTH_TOKEN"),
        help="Bearer token for the dashboard; defaults to HINDSIGHT_DASHBOARD_AUTH_TOKEN.",
    )
    args = parser.parse_args()

    run_dashboard_server(
        host=args.host,
        port=args.port,
        namespace=args.namespace,
        db_url=args.db_url,
        auth_token=args.auth_token,
    )


if __name__ == "__main__":
    main()
