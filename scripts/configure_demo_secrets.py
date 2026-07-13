"""Seed durable demo SecureStrings without placing values in Terraform state."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stage", default="demo")
    parser.add_argument("--expected-gemini-keys", type=int, default=5)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv()

    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        parser.error("DATABASE_URL is required")
    gemini_keys = _gemini_keys(os.environ)
    if len(gemini_keys) != args.expected_gemini_keys:
        parser.error(
            f"expected {args.expected_gemini_keys} Gemini keys, found {len(gemini_keys)}"
        )

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ssm = session.client("ssm", config=aws_client_config(read_timeout=10))
    prefix = f"/hindsight/{args.stage}"
    desired = {
        f"{prefix}/database-url": database_url,
        f"{prefix}/gemini-api-keys": json.dumps(
            {
                "version": 1,
                "keys": [
                    {"id": f"gemini-{index + 1}", "api_key": key}
                    for index, key in enumerate(gemini_keys)
                ],
            },
            separators=(",", ":"),
        ),
        f"{prefix}/operator-token": (
            os.environ.get("HINDSIGHT_FUNCTION_AUTH_TOKEN") or secrets.token_urlsafe(32)
        ),
        f"{prefix}/changefeed-token": (
            os.environ.get("HINDSIGHT_CHANGEFEED_AUTH_TOKEN") or secrets.token_urlsafe(32)
        ),
    }

    for name, value in desired.items():
        exists = _parameter_exists(ssm, name)
        is_generated_token = name.endswith(("operator-token", "changefeed-token")) and not (
            os.environ.get("HINDSIGHT_FUNCTION_AUTH_TOKEN")
            if name.endswith("operator-token")
            else os.environ.get("HINDSIGHT_CHANGEFEED_AUTH_TOKEN")
        )
        if exists and (not args.overwrite or is_generated_token):
            print(f"secure parameter preserved: {name}")
            continue
        if args.dry_run:
            action = "update" if exists else "create"
            print(f"secure parameter would {action}: {name}")
            continue
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="SecureString",
            Tier="Standard",
            Overwrite=exists,
        )
        action = "updated" if exists else "created"
        print(f"secure parameter {action}: {name}")


def _gemini_keys(environ: dict[str, str] | os._Environ[str]) -> list[str]:
    names = ["GEMINI_API_KEY", *(f"GEMINI_API_KEY_{index}" for index in range(1, 20))]
    return [str(environ[name]).strip() for name in names if environ.get(name)]


def _parameter_exists(client: Any, name: str) -> bool:
    try:
        client.get_parameter(Name=name, WithDecryption=False)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return False
        raise
    return True


if __name__ == "__main__":
    main()
