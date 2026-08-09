"""Obtain a short-lived Cognito access token for privileged hosted acceptance."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Any

import boto3
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--username-env", default="HINDSIGHT_OPERATOR_USERNAME")
    parser.add_argument("--password-env", default="HINDSIGHT_OPERATOR_PASSWORD")
    args = parser.parse_args()
    load_dotenv()

    client = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    ).client("cognito-idp", config=aws_client_config(read_timeout=10))
    token = admin_access_token(
        client,
        user_pool_id=_required(args.user_pool_id, "user pool id"),
        client_id=_required(args.client_id, "client id"),
        username=_required(os.environ.get(args.username_env), args.username_env),
        password=_required(os.environ.get(args.password_env), args.password_env),
    )
    sys.stdout.write(token)


def admin_access_token(
    client: Any,
    *,
    user_pool_id: str,
    client_id: str,
    username: str,
    password: str,
) -> str:
    """Authenticate through the IAM-only Cognito admin flow."""

    response = client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    if response.get("ChallengeName"):
        raise RuntimeError("Cognito acceptance identity requires an unsupported challenge")
    result = response.get("AuthenticationResult")
    token = result.get("AccessToken") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Cognito did not return an access token")
    return token.strip()


def _required(value: str | None, label: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


if __name__ == "__main__":
    main()
