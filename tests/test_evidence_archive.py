from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

from hindsight.evidence_archive import EvidenceArchive, canonical_json_bytes


class _Body(io.BytesIO):
    pass


class _S3:
    def __init__(self, *, retention_days: int = 2557):
        self.objects: dict[str, dict[str, object]] = {}
        self.retention_days = retention_days
        self.puts: list[dict[str, object]] = []

    def put_object(self, **kwargs):
        key = str(kwargs["Key"])
        if key in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        version_id = f"version-{len(self.objects) + 1}"
        self.objects[key] = {
            "body": bytes(kwargs["Body"]),
            "version_id": version_id,
            "retain_until": datetime.now(UTC) + timedelta(days=self.retention_days),
        }
        self.puts.append(kwargs)
        return {"VersionId": version_id}

    def head_object(self, **kwargs):
        return {"VersionId": self.objects[str(kwargs["Key"])]["version_id"]}

    def get_object(self, **kwargs):
        stored = self.objects[str(kwargs["Key"])]
        assert kwargs["VersionId"] == stored["version_id"]
        return {"Body": _Body(stored["body"])}

    def get_object_retention(self, **kwargs):
        stored = self.objects[str(kwargs["Key"])]
        assert kwargs["VersionId"] == stored["version_id"]
        return {
            "Retention": {
                "Mode": "GOVERNANCE",
                "RetainUntilDate": stored["retain_until"],
            }
        }


def test_canonical_json_is_sorted_compact_utf8_and_rejects_nan():
    assert canonical_json_bytes({"z": "café", "a": [2, 1]}) == (b'{"a":[2,1],"z":"caf\xc3\xa9"}')
    with pytest.raises(ValueError):
        canonical_json_bytes({"invalid": float("nan")})


def test_bundle_writes_payloads_before_manifest_and_is_idempotent():
    client = _S3()
    archive = EvidenceArchive(bucket="evidence-bucket", client=client)

    first = archive.seal_bundle(
        evidence_id="qualification/run-7/attempt-1",
        objects={"report": {"status": "qualified"}, "provenance": {"sha": "a" * 40}},
        dependencies={"ci": "b" * 64},
    )
    second = archive.seal_bundle(
        evidence_id="qualification/run-7/attempt-1",
        objects={"provenance": {"sha": "a" * 40}, "report": {"status": "qualified"}},
        dependencies={"ci": "b" * 64},
    )

    keys = [str(call["Key"]) for call in client.puts]
    assert keys[-1].endswith("/manifest.json")
    assert first == second
    assert first["manifest_version_id"]
    assert first["manifest_sha256"]
    assert len(client.objects) == 3
    assert all(call["IfNoneMatch"] == "*" for call in client.puts)
    assert all(call["ServerSideEncryption"] == "AES256" for call in client.puts)


def test_existing_different_content_and_short_retention_fail_closed():
    client = _S3()
    archive = EvidenceArchive(bucket="evidence-bucket", client=client)
    archive.put_canonical_json(key="learning/fixed.json", payload={"value": 1})

    with pytest.raises(RuntimeError, match="differs"):
        archive.put_canonical_json(key="learning/fixed.json", payload={"value": 2})

    short = EvidenceArchive(bucket="evidence-bucket", client=_S3(retention_days=30))
    with pytest.raises(RuntimeError, match="shorter than seven years"):
        short.put_canonical_json(key="learning/short.json", payload={"value": 1})


@pytest.mark.parametrize(
    "evidence_id",
    ("", "/leading", "trailing/", "contains/../escape", "Uppercase"),
)
def test_bundle_rejects_unsafe_evidence_identity(evidence_id):
    archive = EvidenceArchive(bucket="evidence-bucket", client=_S3())
    with pytest.raises(ValueError, match="evidence identity"):
        archive.seal_bundle(evidence_id=evidence_id, objects={"report": {}})
