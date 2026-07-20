"""Create and verify immutable, content-addressed learning evidence bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

_EVIDENCE_ID = re.compile(r"[a-z0-9][a-z0-9._/-]{0,239}")
_LOGICAL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_MINIMUM_RETENTION = timedelta(days=2555)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize one JSON value into stable, strict UTF-8 bytes."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class EvidenceArchive:
    """Append and verify deterministic evidence objects in an Object-Locked bucket."""

    def __init__(self, *, bucket: str, client: Any):
        if not bucket or "/" in bucket:
            raise ValueError("a valid evidence bucket name is required")
        self.bucket = bucket
        self.client = client

    def seal_bundle(
        self,
        *,
        evidence_id: str,
        objects: Mapping[str, Any],
        dependencies: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Write payloads before a final manifest and return its verified receipt."""

        self._validate_evidence_id(evidence_id)
        if not objects:
            raise ValueError("an evidence bundle requires at least one object")
        archived = {}
        for logical_name in sorted(objects):
            if not _LOGICAL_NAME.fullmatch(logical_name):
                raise ValueError(f"invalid evidence object name: {logical_name}")
            key = f"learning/evidence/{evidence_id}/{logical_name}.json"
            archived[logical_name] = self.put_canonical_json(
                key=key,
                payload=objects[logical_name],
            )
        manifest = {
            "schema_version": 1,
            "evidence_id": evidence_id,
            "objects": archived,
            "dependencies": dict(sorted((dependencies or {}).items())),
        }
        manifest_record = self.put_canonical_json(
            key=f"learning/evidence/{evidence_id}/manifest.json",
            payload=manifest,
        )
        return {
            "schema_version": 1,
            "evidence_id": evidence_id,
            "bucket": self.bucket,
            "manifest_key": manifest_record["key"],
            "manifest_version_id": manifest_record["version_id"],
            "manifest_sha256": manifest_record["sha256"],
            "retain_until": manifest_record["retain_until"],
        }

    def put_canonical_json(self, *, key: str, payload: Any) -> dict[str, Any]:
        body = canonical_json_bytes(payload)
        digest = sha256_hex(body)
        checksum = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="AES256",
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=checksum,
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
            version_id = str(response.get("VersionId") or "")
            if not version_id:
                raise RuntimeError("evidence archive did not return an object version")
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
            if code not in {"PreconditionFailed", "412"} and status != 412:
                raise
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            version_id = str(head.get("VersionId") or "")
            if not version_id:
                raise RuntimeError("existing evidence object has no version identity") from exc
        observed = self._read_exact(key=key, version_id=version_id)
        if observed != body:
            raise RuntimeError("existing evidence object differs from canonical content")
        retention = (
            self.client.get_object_retention(
                Bucket=self.bucket,
                Key=key,
                VersionId=version_id,
            ).get("Retention")
            or {}
        )
        retain_until = retention.get("RetainUntilDate")
        if retention.get("Mode") != "GOVERNANCE" or not isinstance(retain_until, datetime):
            raise RuntimeError("evidence object does not have Governance retention")
        normalized_until = retain_until.astimezone(UTC)
        if normalized_until < datetime.now(UTC) + _MINIMUM_RETENTION:
            raise RuntimeError("evidence object retention is shorter than seven years")
        return {
            "key": key,
            "version_id": version_id,
            "bytes": len(body),
            "sha256": digest,
            "checksum_sha256": checksum,
            "retain_until": normalized_until.isoformat(),
        }

    def _read_exact(self, *, key: str, version_id: str) -> bytes:
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=key,
            VersionId=version_id,
        )
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    @staticmethod
    def _validate_evidence_id(evidence_id: str) -> None:
        if (
            not _EVIDENCE_ID.fullmatch(evidence_id)
            or evidence_id.startswith("/")
            or evidence_id.endswith("/")
            or ".." in evidence_id.split("/")
        ):
            raise ValueError("invalid evidence identity")
