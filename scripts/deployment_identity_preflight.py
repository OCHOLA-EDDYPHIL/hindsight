"""Reject mixed-account deployment inputs before Terraform touches remote state."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from typing import Any

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402


ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]{12}$")


def _certificate_identity(certificate_arn: str) -> tuple[str, str]:
    parts = certificate_arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or parts[2] != "acm"
        or not parts[3]
        or not ACCOUNT_ID_PATTERN.fullmatch(parts[4])
        or not parts[5].startswith("certificate/")
    ):
        raise RuntimeError("ACM certificate ARN is malformed")
    return parts[3], parts[4]


def verify_deployment_identity(
    *,
    expected_account_id: str,
    region: str,
    state_bucket: str,
    certificate_arn: str,
    sts_client: Any | None = None,
    s3_client: Any | None = None,
    acm_client: Any | None = None,
) -> None:
    if not ACCOUNT_ID_PATTERN.fullmatch(expected_account_id):
        raise RuntimeError("expected AWS account ID must contain exactly 12 digits")

    certificate_region, certificate_account = _certificate_identity(certificate_arn)
    if certificate_region != region or certificate_account != expected_account_id:
        raise RuntimeError("ACM certificate does not belong to the expected account and region")

    config = aws_client_config(read_timeout=10)
    sts = sts_client or boto3.client("sts", region_name=region, config=config)
    s3 = s3_client or boto3.client("s3", region_name=region, config=config)
    acm = acm_client or boto3.client("acm", region_name=region, config=config)

    caller_account = str(sts.get_caller_identity()["Account"])
    if caller_account != expected_account_id:
        raise RuntimeError("configured AWS credentials belong to an unexpected account")

    location = s3.get_bucket_location(
        Bucket=state_bucket,
        ExpectedBucketOwner=expected_account_id,
    ).get("LocationConstraint")
    normalized_location = "us-east-1" if location in (None, "") else location
    if normalized_location == "EU":
        normalized_location = "eu-west-1"
    if normalized_location != region:
        raise RuntimeError("Terraform state bucket is in an unexpected region")

    versioning = s3.get_bucket_versioning(
        Bucket=state_bucket,
        ExpectedBucketOwner=expected_account_id,
    )
    if versioning.get("Status") != "Enabled":
        raise RuntimeError("Terraform state bucket must have versioning enabled")

    certificate = acm.describe_certificate(CertificateArn=certificate_arn)["Certificate"]
    if certificate.get("Status") != "ISSUED":
        raise RuntimeError("ACM certificate must be issued before deployment")

    print(
        "deployment identity ready: "
        f"account {expected_account_id}, region {region}, versioned state bucket"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--state-bucket", required=True)
    parser.add_argument("--certificate-arn", required=True)
    args = parser.parse_args()

    verify_deployment_identity(
        expected_account_id=args.expected_account_id,
        region=args.region,
        state_bucket=args.state_bucket,
        certificate_arn=args.certificate_arn,
    )


if __name__ == "__main__":
    main()
