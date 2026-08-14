"""Redrive one exhausted run from the protected quarantine ledger."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.quarantine import quarantine_table_from_env  # noqa: E402
from hindsight.quarantine_redrive import (  # noqa: E402
    QuarantineRedriveError,
    redrive_quarantined_run,
)
from hindsight.runtime import runtime_database_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quarantine-id", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        repository = _required_environment("GITHUB_REPOSITORY")
        repository_owner, separator, _repository_name = repository.partition("/")
        if not separator or not repository_owner:
            raise QuarantineRedriveError("GitHub repository identity is invalid")
        actor = _required_environment("GITHUB_ACTOR")
        triggering_actor = _required_environment("GITHUB_TRIGGERING_ACTOR")
        if actor != repository_owner or triggering_actor != repository_owner:
            raise QuarantineRedriveError("quarantine redrive requires the repository owner")
        result = redrive_quarantined_run(
            table=quarantine_table_from_env(),
            quarantine_id=args.quarantine_id,
            raw_body_sha256=args.digest,
            confirmation=args.confirm,
            repository_owner=repository_owner,
            actor=actor,
            triggering_actor=triggering_actor,
            db_url=runtime_database_url(),
        )
    except QuarantineRedriveError as exc:
        print(f"quarantine redrive refused: {exc}", file=sys.stderr)
        return 2
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "AwsClientError")
        print(f"quarantine AWS operation failed: {code}", file=sys.stderr)
        return 3
    except Exception:
        print("quarantine redrive failed", file=sys.stderr)
        return 4
    print(json.dumps(result, sort_keys=True))
    return 0


def _required_environment(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise QuarantineRedriveError(f"{name} is required")
    return value


if __name__ == "__main__":
    sys.exit(main())
