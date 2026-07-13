"""Fail before application apply when durable deployment dependencies are unavailable."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request

import boto3
import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.gemini import parse_gemini_credentials  # noqa: E402
from hindsight.runtime import DATABASE_URL_PARAM_ENV, runtime_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--certificate-arn", required=True)
    parser.add_argument("--cloudflare-zone-id", required=True)
    parser.add_argument("--database-parameter", required=True)
    parser.add_argument("--gemini-parameter", required=True)
    parser.add_argument("--operator-parameter", required=True)
    parser.add_argument("--changefeed-parameter", required=True)
    args = parser.parse_args()

    ssm = boto3.client("ssm", region_name=args.region, config=aws_client_config(read_timeout=10))
    names = [
        args.database_parameter,
        args.gemini_parameter,
        args.operator_parameter,
        args.changefeed_parameter,
    ]
    response = ssm.get_parameters(Names=names, WithDecryption=True)
    if response.get("InvalidParameters"):
        missing = ", ".join(response["InvalidParameters"])
        raise RuntimeError(f"missing SSM parameters: {missing}")
    values = {parameter["Name"]: parameter["Value"] for parameter in response["Parameters"]}
    credentials = parse_gemini_credentials({"GEMINI_API_KEYS": values[args.gemini_parameter]})
    if not credentials:
        raise RuntimeError("Gemini key pool is empty")
    print(f"preflight: {len(names)} SSM parameters and {len(credentials)} Gemini slots ready")

    acm = boto3.client("acm", region_name=args.region, config=aws_client_config(read_timeout=10))
    certificate = acm.describe_certificate(CertificateArn=args.certificate_arn)["Certificate"]
    if certificate.get("Status") != "ISSUED":
        raise RuntimeError(f"ACM certificate is {certificate.get('Status')}, expected ISSUED")
    print("preflight: ACM certificate issued")

    token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("CLOUDFLARE_API_TOKEN is required")
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{args.cloudflare_zone_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as result:
        payload = json.load(result)
    if not payload.get("success") or payload.get("result", {}).get("status") != "active":
        raise RuntimeError("Cloudflare zone is not active or accessible")
    print("preflight: Cloudflare zone active")

    settings = runtime_settings(
        environ={
            DATABASE_URL_PARAM_ENV: args.database_parameter,
            "LLM_PROVIDER": "deterministic",
            "AWS_REGION": args.region,
        },
        ssm_client=ssm,
        use_cache=False,
    )
    with psycopg.connect(
        settings.database_url,
        connect_timeout=5,
        application_name="hindsight-deployment-preflight",
    ) as connection:
        value = connection.execute("SELECT 1").fetchone()[0]
    if value != 1:
        raise RuntimeError("CockroachDB readiness query returned an unexpected result")
    print("preflight: CockroachDB reachable")


if __name__ == "__main__":
    main()
