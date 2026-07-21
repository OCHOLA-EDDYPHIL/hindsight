"""KMS-backed opaque identifiers for protected learning evidence."""

from __future__ import annotations

import re
from typing import Any

import boto3

from hindsight.aws import aws_client_config

_TOKEN_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_TOKEN_DOMAIN = "hindsight-qualification-token-v1"


class KmsHmacTokenizer:
    """Generate domain-separated identifiers without exposing the HMAC key."""

    def __init__(self, *, key_id: str, family_sha256: str, client: Any | None = None):
        if not key_id:
            raise ValueError("a KMS HMAC key is required")
        if len(family_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in family_sha256
        ):
            raise ValueError("a scientific-family digest is required")
        self.key_id = key_id
        self.family_sha256 = family_sha256
        self.client = client or boto3.client("kms", config=aws_client_config())

    def token(self, *, kind: str, raw_id: str) -> str:
        if not _TOKEN_KIND.fullmatch(kind):
            raise ValueError("invalid opaque-token kind")
        if not raw_id:
            raise ValueError("opaque-token source identity is required")
        message = "\x1f".join((_TOKEN_DOMAIN, self.family_sha256, kind, raw_id)).encode("utf-8")
        response = self.client.generate_mac(
            KeyId=self.key_id,
            Message=message,
            MacAlgorithm="HMAC_SHA_256",
        )
        mac = bytes(response["Mac"])
        if len(mac) != 32:
            raise RuntimeError("KMS returned an invalid HMAC-SHA256 value")
        return mac.hex()
