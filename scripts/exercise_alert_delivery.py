"""Prove exact-release CloudWatch alarm delivery through the controlled receiver."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

FULL_SHA = re.compile(r"[0-9a-f]{40}")
ACCOUNT_ID = re.compile(r"[0-9]{12}")
STAGE = re.compile(r"[a-z][a-z0-9-]{1,15}")
CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=15,
    retries={"total_max_attempts": 2, "mode": "standard"},
)
DEFAULT_TIMEOUT_SECONDS = 90


def _parse_arn(value: str, *, service: str, label: str) -> dict[str, str]:
    parts = value.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or not parts[1]
        or parts[2] != service
        or not parts[3]
        or ACCOUNT_ID.fullmatch(parts[4]) is None
        or not parts[5]
    ):
        raise ValueError(f"{label} must be a complete AWS {service} ARN")
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account_id": parts[4],
        "resource": parts[5],
    }


def _client(session: Any, service: str, *, region: str) -> Any:
    return session.client(service, region_name=region, config=CLIENT_CONFIG)


def _confirmed_subscription(
    client: Any,
    *,
    topic_arn: str,
    receiver_queue_arn: str,
) -> dict[str, str]:
    matches: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_subscriptions_by_topic")
    for page in paginator.paginate(TopicArn=topic_arn):
        for subscription in page.get("Subscriptions", []):
            if (
                subscription.get("Protocol") == "sqs"
                and subscription.get("Endpoint") == receiver_queue_arn
            ):
                matches.append(subscription)
    if len(matches) != 1:
        raise RuntimeError("alert topic must have exactly one controlled SQS receiver subscription")
    subscription_arn = str(matches[0].get("SubscriptionArn") or "")
    if subscription_arn in {"", "PendingConfirmation", "Deleted"}:
        raise RuntimeError("controlled alert receiver subscription is not confirmed")
    subscription = _parse_arn(
        subscription_arn,
        service="sns",
        label="subscription ARN",
    )
    topic = _parse_arn(topic_arn, service="sns", label="topic ARN")
    if (
        subscription["partition"] != topic["partition"]
        or subscription["region"] != topic["region"]
        or subscription["account_id"] != topic["account_id"]
        or not subscription["resource"].startswith(f"{topic['resource']}:")
    ):
        raise RuntimeError("controlled alert subscription is outside the exact topic")
    attributes = client.get_subscription_attributes(SubscriptionArn=subscription_arn).get(
        "Attributes", {}
    )
    if str(attributes.get("PendingConfirmation", "false")).lower() == "true":
        raise RuntimeError("controlled alert receiver subscription remains pending")
    if attributes.get("TopicArn") not in {None, topic_arn}:
        raise RuntimeError("controlled alert subscription topic does not match")
    if attributes.get("Endpoint") not in {None, receiver_queue_arn}:
        raise RuntimeError("controlled alert subscription endpoint does not match")
    return {
        "topic_arn": topic_arn,
        "subscription_arn": subscription_arn,
        "protocol": "sqs",
        "endpoint": receiver_queue_arn,
        "status": "confirmed",
    }


def _receiver_queue(
    client: Any,
    *,
    queue_name: str,
    expected_account_id: str,
    expected_region: str,
) -> tuple[str, str]:
    response = client.get_queue_url(
        QueueName=queue_name,
        QueueOwnerAWSAccountId=expected_account_id,
    )
    queue_url = str(response.get("QueueUrl") or "")
    if not queue_url.startswith("https://"):
        raise RuntimeError("controlled alert receiver URL is unavailable")
    attributes = client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["QueueArn", "KmsMasterKeyId", "SqsManagedSseEnabled"],
    ).get("Attributes", {})
    queue_arn = str(attributes.get("QueueArn") or "")
    parsed = _parse_arn(queue_arn, service="sqs", label="receiver queue ARN")
    if (
        parsed["account_id"] != expected_account_id
        or parsed["region"] != expected_region
        or parsed["resource"] != queue_name
    ):
        raise RuntimeError("controlled alert receiver identity does not match")
    if (
        str(attributes.get("SqsManagedSseEnabled", "false")).lower() != "true"
        and not str(attributes.get("KmsMasterKeyId") or "").strip()
    ):
        raise RuntimeError("controlled alert receiver is not encrypted")
    return queue_url, queue_arn


def _release_alarm(
    client: Any,
    *,
    alarm_name: str,
    operational_topic_arn: str,
    source_revision: str,
    stage: str,
    expected_account_id: str,
    expected_region: str,
) -> dict[str, Any]:
    alarms = client.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
    if len(alarms) != 1 or alarms[0].get("AlarmName") != alarm_name:
        raise RuntimeError("exact-release probe alarm is unavailable")
    alarm = alarms[0]
    alarm_arn = str(alarm.get("AlarmArn") or "")
    parsed = _parse_arn(alarm_arn, service="cloudwatch", label="alarm ARN")
    if (
        parsed["account_id"] != expected_account_id
        or parsed["region"] != expected_region
        or parsed["resource"] != f"alarm:{alarm_name}"
    ):
        raise RuntimeError("exact-release probe alarm identity does not match")
    if (
        alarm.get("Namespace") != "Hindsight/Release"
        or alarm.get("MetricName") != "ExactReleaseProbe"
    ):
        raise RuntimeError("exact-release probe alarm metric does not match")
    dimensions = {
        str(dimension.get("Name")): str(dimension.get("Value"))
        for dimension in alarm.get("Dimensions", [])
    }
    if dimensions != {"ReleaseRevision": source_revision, "Stage": stage}:
        raise RuntimeError("exact-release probe alarm is not bound to this release")
    for actions_key in ("AlarmActions", "OKActions"):
        if operational_topic_arn not in alarm.get(actions_key, []):
            raise RuntimeError("exact-release probe alarm is missing an operational topic action")
    return {
        "alarm_name": alarm_name,
        "alarm_arn": alarm_arn,
        "release_revision": source_revision,
        "stage": stage,
        "initial_state": str(alarm.get("StateValue") or "INSUFFICIENT_DATA"),
    }


def _set_alarm_state(
    client: Any,
    *,
    alarm_name: str,
    state: str,
    challenge: str,
    source_revision: str,
) -> None:
    reason = f"hindsight exact-release challenge={challenge};state={state}"
    client.set_alarm_state(
        AlarmName=alarm_name,
        StateValue=state,
        StateReason=reason,
        StateReasonData=json.dumps(
            {
                "challenge": challenge,
                "source_revision": source_revision,
                "state": state,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _matching_alarm_message(
    body: str,
    *,
    alarm_name: str,
    challenge: str,
    expected_state: str,
    operational_topic_arn: str,
) -> dict[str, str] | None:
    try:
        notification = json.loads(body)
        message = json.loads(notification["Message"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    expected_reason = f"hindsight exact-release challenge={challenge};state={expected_state}"
    if (
        notification.get("Type") != "Notification"
        or notification.get("TopicArn") != operational_topic_arn
        or message.get("AlarmName") != alarm_name
        or message.get("NewStateValue") != expected_state
        or message.get("NewStateReason") != expected_reason
    ):
        return None
    return {
        "sns_message_id": str(notification.get("MessageId") or ""),
        "state": expected_state,
        "state_change_time": str(message.get("StateChangeTime") or ""),
    }


def _receive_and_delete(
    client: Any,
    *,
    queue_url: str,
    alarm_name: str,
    challenge: str,
    expected_state: str,
    operational_topic_arn: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        response = client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            MessageSystemAttributeNames=["All"],
            MessageAttributeNames=["All"],
            VisibilityTimeout=min(300, remaining),
            WaitTimeSeconds=min(10, remaining),
        )
        for received in response.get("Messages", []):
            match = _matching_alarm_message(
                str(received.get("Body") or ""),
                alarm_name=alarm_name,
                challenge=challenge,
                expected_state=expected_state,
                operational_topic_arn=operational_topic_arn,
            )
            if match is None:
                continue
            receipt_handle = str(received.get("ReceiptHandle") or "")
            if not receipt_handle:
                raise RuntimeError("controlled alert message has no receipt handle")
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
            return {
                **match,
                "sqs_message_id": str(received.get("MessageId") or ""),
                "deleted": True,
            }
    raise RuntimeError(f"controlled receiver did not deliver the {expected_state} alarm")


def exercise(
    *,
    alarm_name: str,
    receiver_queue_name: str,
    operational_topic_arn: str,
    budget_topic_arn: str,
    expected_account_id: str,
    region: str,
    stage: str,
    profile: str | None,
    source_revision: str,
    session: Any = None,
    challenge: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if FULL_SHA.fullmatch(source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    if ACCOUNT_ID.fullmatch(expected_account_id) is None:
        raise ValueError("expected account ID must contain twelve digits")
    if STAGE.fullmatch(stage) is None:
        raise ValueError("stage is invalid")
    if timeout_seconds < 1 or timeout_seconds > 300:
        raise ValueError("timeout must be between 1 and 300 seconds")
    expected_alarm_name = f"hindsight-{stage}-exact-release-probe"
    expected_queue_name = f"hindsight-{stage}-alert-receiver"
    if alarm_name != expected_alarm_name or receiver_queue_name != expected_queue_name:
        raise ValueError("alarm and receiver names must match the exact stage")

    operational_topic = _parse_arn(
        operational_topic_arn,
        service="sns",
        label="operational topic ARN",
    )
    budget_topic = _parse_arn(
        budget_topic_arn,
        service="sns",
        label="budget topic ARN",
    )
    if (
        operational_topic["account_id"] != expected_account_id
        or operational_topic["region"] != region
        or operational_topic["resource"] != f"hindsight-{stage}-alerts"
        or budget_topic["account_id"] != expected_account_id
        or budget_topic["region"] != "us-east-1"
        or budget_topic["resource"] != f"hindsight-{stage}-budget-alerts"
        or operational_topic["partition"] != budget_topic["partition"]
    ):
        raise ValueError("alert topics must match the exact account, region, and stage")

    resolved_session = session or boto3.Session(profile_name=profile or None)
    sqs_client = _client(resolved_session, "sqs", region=region)
    cloudwatch_client = _client(resolved_session, "cloudwatch", region=region)
    sns_clients = {
        topic_region: _client(resolved_session, "sns", region=topic_region)
        for topic_region in {operational_topic["region"], budget_topic["region"]}
    }
    queue_url, queue_arn = _receiver_queue(
        sqs_client,
        queue_name=receiver_queue_name,
        expected_account_id=expected_account_id,
        expected_region=region,
    )
    subscriptions = [
        _confirmed_subscription(
            sns_clients[topic["region"]],
            topic_arn=topic_arn,
            receiver_queue_arn=queue_arn,
        )
        for topic, topic_arn in (
            (operational_topic, operational_topic_arn),
            (budget_topic, budget_topic_arn),
        )
    ]
    alarm = _release_alarm(
        cloudwatch_client,
        alarm_name=alarm_name,
        operational_topic_arn=operational_topic_arn,
        source_revision=source_revision,
        stage=stage,
        expected_account_id=expected_account_id,
        expected_region=region,
    )

    resolved_challenge = challenge or secrets.token_hex(16)
    if re.fullmatch(r"[0-9a-f]{32}", resolved_challenge) is None:
        raise ValueError("challenge must contain 32 lowercase hexadecimal characters")
    preparation_challenge = secrets.token_hex(16)
    if alarm["initial_state"] != "OK":
        _set_alarm_state(
            cloudwatch_client,
            alarm_name=alarm_name,
            state="OK",
            challenge=preparation_challenge,
            source_revision=source_revision,
        )
        _receive_and_delete(
            sqs_client,
            queue_url=queue_url,
            alarm_name=alarm_name,
            challenge=preparation_challenge,
            expected_state="OK",
            operational_topic_arn=operational_topic_arn,
            timeout_seconds=timeout_seconds,
        )
    alarm_receipt: dict[str, Any] | None = None
    try:
        _set_alarm_state(
            cloudwatch_client,
            alarm_name=alarm_name,
            state="ALARM",
            challenge=resolved_challenge,
            source_revision=source_revision,
        )
        alarm_receipt = _receive_and_delete(
            sqs_client,
            queue_url=queue_url,
            alarm_name=alarm_name,
            challenge=resolved_challenge,
            expected_state="ALARM",
            operational_topic_arn=operational_topic_arn,
            timeout_seconds=timeout_seconds,
        )
    finally:
        _set_alarm_state(
            cloudwatch_client,
            alarm_name=alarm_name,
            state="OK",
            challenge=resolved_challenge,
            source_revision=source_revision,
        )
    if alarm_receipt is None:
        raise RuntimeError("controlled ALARM receipt was not recorded")
    ok_receipt = _receive_and_delete(
        sqs_client,
        queue_url=queue_url,
        alarm_name=alarm_name,
        challenge=resolved_challenge,
        expected_state="OK",
        operational_topic_arn=operational_topic_arn,
        timeout_seconds=timeout_seconds,
    )
    return {
        "schema_version": 2,
        "kind": "alert_delivery_evidence",
        "source_revision": source_revision,
        "account_id": expected_account_id,
        "region": region,
        "stage": stage,
        "challenge": resolved_challenge,
        "alarm": alarm,
        "receiver": {
            "queue_arn": queue_arn,
            "encrypted": True,
            "subscriptions": subscriptions,
        },
        "transitions": [alarm_receipt, ok_receipt],
        "exercised_at": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alarm-name", required=True)
    parser.add_argument("--receiver-queue-name", required=True)
    parser.add_argument("--operational-topic-arn", required=True)
    parser.add_argument("--budget-topic-arn", required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = exercise(
            alarm_name=args.alarm_name,
            receiver_queue_name=args.receiver_queue_name,
            operational_topic_arn=args.operational_topic_arn,
            budget_topic_arn=args.budget_topic_arn,
            expected_account_id=args.expected_account_id,
            region=args.region,
            stage=args.stage,
            profile=args.profile,
            source_revision=args.source_revision,
            timeout_seconds=args.timeout_seconds,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        return 0
    except (
        BotoCoreError,
        ClientError,
        OSError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"alert delivery exercise failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
