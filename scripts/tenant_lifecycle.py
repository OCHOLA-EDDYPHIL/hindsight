"""Run privileged, fenced tenant export and purge operations."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from dataclasses import asdict
from typing import Any, Mapping
from uuid import UUID, uuid4

import boto3
import psycopg
from botocore.exceptions import ClientError
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.lifecycle import (  # noqa: E402
    LifecycleError,
    LifecycleOperation,
    abort_operation,
    begin_export,
    begin_purge,
    connect_lifecycle,
    finalize_purge,
    get_operation,
    get_tombstone,
    heartbeat_lease,
    purge_database_tenant,
    record_principal_cleanup_targets,
    record_verified_export,
)
from hindsight.lifecycle_aws import (  # noqa: E402
    AwsTenantCleaner,
    export_tenant_to_s3,
    verify_stored_export,
)

DATABASE_URL_ENV = "DATABASE_URL"
EXPORT_BUCKET_ENV = "HINDSIGHT_LIFECYCLE_EXPORT_BUCKET"
TICKET_TABLE_ENV = "HINDSIGHT_REALTIME_TICKET_TABLE"
SUBSCRIPTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_SUBSCRIPTION_TABLE"
CONNECTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_CONNECTION_TABLE"
MANAGEMENT_ENDPOINT_ENV = "HINDSIGHT_WEBSOCKET_MANAGEMENT_ENDPOINT"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        database_url = _required_environment(DATABASE_URL_ENV)
        if args.command == "export":
            _run_export(args, database_url=database_url)
        elif args.command == "verify":
            _run_verify(args, database_url=database_url)
        elif args.command == "purge":
            _run_purge(args, database_url=database_url)
        elif args.command == "status":
            _run_status(args, database_url=database_url)
        elif args.command == "abort":
            _run_abort(args, database_url=database_url)
        else:  # pragma: no cover - argparse enforces the command set
            parser.error("unsupported lifecycle command")
    except LifecycleError as exc:
        print(f"lifecycle operation refused: {exc}", file=sys.stderr)
        return 2
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "AwsClientError")
        print(f"lifecycle AWS operation failed: {code}", file=sys.stderr)
        return 3
    except psycopg.Error:
        print("lifecycle database operation failed", file=sys.stderr)
        return 4
    except (KeyError, TypeError, ValueError) as exc:
        print(f"lifecycle input is invalid: {exc}", file=sys.stderr)
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export")
    export.add_argument("--tenant-id", required=True, type=_uuid)
    export.add_argument("--operation-id", type=_uuid)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--operation-id", required=True, type=_uuid)

    purge = subparsers.add_parser("purge")
    purge.add_argument("--operation-id", required=True, type=_uuid)
    purge.add_argument("--confirm-fingerprint", required=True, type=_fingerprint)

    status = subparsers.add_parser("status")
    status.add_argument("--operation-id", required=True, type=_uuid)

    abort = subparsers.add_parser("abort")
    abort.add_argument("--operation-id", required=True, type=_uuid)
    return parser


def _run_export(args: argparse.Namespace, *, database_url: str) -> None:
    bucket = _required_environment(EXPORT_BUCKET_ENV)
    operation_id = args.operation_id or str(uuid4())
    print(f"lifecycle export operation id: {operation_id}", file=sys.stderr)
    session = _aws_session(args)
    s3_client = session.client("s3", config=aws_client_config(read_timeout=60))
    with connect_lifecycle(database_url) as state_connection:
        preparation = begin_export(
            state_connection,
            tenant_id=args.tenant_id,
            operation_id=operation_id,
        )
        with connect_lifecycle(database_url) as read_connection:
            result = export_tenant_to_s3(
                read_connection=read_connection,
                state_connection=state_connection,
                preparation=preparation,
                s3_client=s3_client,
                bucket=bucket,
                heartbeat=lambda: heartbeat_lease(
                    state_connection,
                    operation_id=preparation.operation.id,
                    lease_owner=preparation.lease_owner,
                ),
            )
    _print_json(
        {
            "export_fingerprint": result.fingerprint,
            "operation_id": preparation.operation.id,
            "status": "exported",
        }
    )


def _run_verify(args: argparse.Namespace, *, database_url: str) -> None:
    session = _aws_session(args)
    s3_client = session.client("s3", config=aws_client_config(read_timeout=60))
    with connect_lifecycle(database_url) as connection:
        operation = _required_operation(connection, args.operation_id)
        result = verify_stored_export(
            connection=connection,
            operation=operation,
            s3_client=s3_client,
        )
        record_verified_export(
            connection,
            operation_id=operation.id,
            fingerprint=result["fingerprint"],
        )
    _print_json(
        {
            "export_fingerprint": result["fingerprint"],
            "operation_id": operation.id,
            "record_count": result["record_count"],
            "status": "verified",
            "table_count": result["table_count"],
        }
    )


def _run_purge(args: argparse.Namespace, *, database_url: str) -> None:
    with connect_lifecycle(database_url) as connection:
        exported_operation = get_operation(connection, args.operation_id)
        if exported_operation is None:
            completed = get_tombstone(connection, args.operation_id)
            if completed is None:
                raise LifecycleError("lifecycle operation does not exist")
            if completed["export_fingerprint"] != args.confirm_fingerprint:
                raise LifecycleError(
                    "confirmed fingerprint does not match the completed purge"
                )
            _print_json(
                {
                    "operation_id": completed["purge_id"],
                    "purged_at": completed["purged_at"],
                    "status": "completed",
                    "tenant_identity_sha256": completed[
                        "tenant_identity_sha256"
                    ],
                }
            )
            return
        session = _aws_session(args)
        cleaner = _aws_cleaner(session)
        s3_client = session.client("s3", config=aws_client_config(read_timeout=60))
        if exported_operation.status in {"purging", "database_purged"}:
            if (
                exported_operation.export_fingerprint != args.confirm_fingerprint
                or exported_operation.confirmed_export_fingerprint
                != args.confirm_fingerprint
            ):
                raise LifecycleError(
                    "confirmed fingerprint does not match the interrupted purge"
                )
        else:
            verification = verify_stored_export(
                connection=connection,
                operation=exported_operation,
                s3_client=s3_client,
            )
            if verification["fingerprint"] != args.confirm_fingerprint:
                raise LifecycleError("confirmed fingerprint does not match the stored export")
        operation = begin_purge(
            connection,
            operation_id=args.operation_id,
            confirmed_fingerprint=args.confirm_fingerprint,
        )
        lease_owner = operation.lease_owner
        if lease_owner is None:
            raise LifecycleError("purge did not acquire a lease")
        cleanup_targets = record_principal_cleanup_targets(
            connection,
            operation_id=operation.id,
            lease_owner=lease_owner,
        )
        cleanup = cleaner.cleanup(
            tenant_id=operation.target_tenant_id,
            cognito_credential_locators=(
                cleanup_targets.cognito_credential_locators
            ),
            heartbeat=lambda: heartbeat_lease(
                connection,
                operation_id=operation.id,
                lease_owner=lease_owner,
            ),
        )
        if operation.status != "database_purged":
            operation = purge_database_tenant(
                connection,
                operation_id=operation.id,
                lease_owner=lease_owner,
            )
        cleaner.assert_clean(
            tenant_id=operation.target_tenant_id,
            cognito_credential_locators=(
                cleanup_targets.cognito_credential_locators
            ),
        )
        heartbeat_lease(
            connection,
            operation_id=operation.id,
            lease_owner=lease_owner,
        )
        tombstone = finalize_purge(
            connection,
            operation_id=operation.id,
            lease_owner=lease_owner,
        )
    _print_json(
        {
            "cleanup": asdict(cleanup),
            "operation_id": tombstone["purge_id"],
            "purged_at": tombstone["purged_at"],
            "status": "completed",
            "tenant_identity_sha256": tombstone["tenant_identity_sha256"],
        }
    )


def _run_status(args: argparse.Namespace, *, database_url: str) -> None:
    with connect_lifecycle(database_url) as connection:
        operation = get_operation(connection, args.operation_id)
        if operation is not None:
            _print_json(_operation_status(operation))
            return
        tombstone = get_tombstone(connection, args.operation_id)
    if tombstone is None:
        raise LifecycleError("lifecycle operation does not exist")
    _print_json(
        {
            "database_purged_at": tombstone["database_purged_at"],
            "export_fingerprint": tombstone["export_fingerprint"],
            "operation_id": tombstone["purge_id"],
            "purged_at": tombstone["purged_at"],
            "schema_identity_sha256": tombstone["schema_identity_sha256"],
            "status": "completed",
            "tenant_identity_sha256": tombstone["tenant_identity_sha256"],
        }
    )


def _run_abort(args: argparse.Namespace, *, database_url: str) -> None:
    with connect_lifecycle(database_url) as connection:
        operation = abort_operation(connection, operation_id=args.operation_id)
    _print_json(
        {
            "operation_id": operation.id,
            "status": operation.status,
            "tenant_identity_sha256": operation.tenant_identity_sha256,
        }
    )


def _aws_cleaner(session: boto3.Session) -> AwsTenantCleaner:
    config = aws_client_config(read_timeout=30)
    dynamodb = session.resource("dynamodb", config=config)
    endpoint = _required_environment(MANAGEMENT_ENDPOINT_ENV)
    return AwsTenantCleaner(
        ticket_table=dynamodb.Table(_required_environment(TICKET_TABLE_ENV)),
        subscription_table=dynamodb.Table(_required_environment(SUBSCRIPTION_TABLE_ENV)),
        connection_table=dynamodb.Table(_required_environment(CONNECTION_TABLE_ENV)),
        cognito_client=session.client("cognito-idp", config=config),
        websocket_client=session.client(
            "apigatewaymanagementapi",
            endpoint_url=endpoint,
            config=config,
        ),
    )


def _aws_session(args: argparse.Namespace) -> boto3.Session:
    return boto3.Session(profile_name=args.profile, region_name=args.region)


def _required_operation(
    connection: psycopg.Connection, operation_id: str
) -> LifecycleOperation:
    operation = get_operation(connection, operation_id)
    if operation is None:
        raise LifecycleError("lifecycle operation does not exist")
    return operation


def _operation_status(operation: LifecycleOperation) -> dict[str, Any]:
    return {
        "database_purged_at": _isoformat(operation.database_purged_at),
        "export_fingerprint": operation.export_fingerprint,
        "export_retention_until": _isoformat(operation.export_retention_until),
        "export_verified_at": _isoformat(operation.export_verified_at),
        "lease_expires_at": _isoformat(operation.lease_expires_at),
        "operation_id": operation.id,
        "schema_identity_sha256": operation.schema_identity_sha256,
        "status": operation.status,
        "tenant_identity_sha256": operation.tenant_identity_sha256,
    }


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _fingerprint(value: str) -> str:
    normalized = value.strip()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 digest")
    return normalized


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _print_json(value: Mapping[str, Any] | dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    sys.exit(main())
