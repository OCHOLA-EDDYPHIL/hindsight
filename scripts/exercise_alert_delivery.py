"""Publish one explicit test notification and record provider acknowledgement."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3


def publish(
    *,
    topic_arn: str,
    profile: str | None,
    source_revision: str,
    session: Any = None,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    arn = topic_arn.split(":", 5)
    if len(arn) != 6 or arn[:3] != ["arn", "aws", "sns"] or not arn[3] or not arn[4]:
        raise ValueError("topic ARN must identify an AWS SNS topic")
    resolved_session = session or boto3.Session(profile_name=profile or None)
    response = resolved_session.client("sns", region_name=arn[3]).publish(
        TopicArn=topic_arn,
        Subject="Hindsight alert delivery exercise",
        Message=json.dumps({"kind": "alert_delivery_exercise", "source_revision": source_revision}),
    )
    message_id = str(response.get("MessageId") or "").strip()
    if not message_id:
        raise RuntimeError("SNS did not acknowledge the alert delivery exercise")
    return {
        "schema_version": 1,
        "kind": "alert_delivery_evidence",
        "source_revision": source_revision,
        "account_id": arn[4],
        "region": arn[3],
        "topic_arn": topic_arn,
        "message_id": message_id,
        "published_at": datetime.now(UTC).isoformat(),
        "limitation": "SNS acknowledgement does not prove an unconfirmed endpoint received the message.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic-arn", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = publish(
        topic_arn=args.topic_arn,
        profile=args.profile,
        source_revision=args.source_revision,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
