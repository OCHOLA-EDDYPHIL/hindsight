"""Shared AWS client configuration."""

from __future__ import annotations

from botocore.config import Config

AWS_CONNECT_TIMEOUT_SECONDS = 3
AWS_READ_TIMEOUT_SECONDS = 20
AWS_MAX_ATTEMPTS = 2


def aws_client_config(
    *,
    connect_timeout: int = AWS_CONNECT_TIMEOUT_SECONDS,
    read_timeout: int = AWS_READ_TIMEOUT_SECONDS,
    max_attempts: int = AWS_MAX_ATTEMPTS,
) -> Config:
    """Return bounded timeout/retry settings for boto3 clients."""

    return Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"max_attempts": max_attempts, "mode": "standard"},
    )
