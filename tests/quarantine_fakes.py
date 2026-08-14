from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class ConditionalCheckFailedException(Exception):
    pass


class InMemoryQuarantineTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.meta = SimpleNamespace(
            client=SimpleNamespace(
                exceptions=SimpleNamespace(
                    ConditionalCheckFailedException=ConditionalCheckFailedException,
                )
            )
        )

    def put_item(self, *, Item, ConditionExpression):
        del ConditionExpression
        key = Item["quarantine_id"]
        if key in self.items:
            raise ConditionalCheckFailedException()
        self.items[key] = dict(Item)
        return {}

    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead is True
        item = self.items.get(Key["quarantine_id"])
        return {"Item": dict(item)} if item is not None else {}

    def update_item(
        self,
        *,
        Key,
        UpdateExpression,
        ConditionExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues,
        ReturnValues,
    ):
        del UpdateExpression, ConditionExpression, ExpressionAttributeNames
        assert ReturnValues == "ALL_NEW"
        item = self.items[Key["quarantine_id"]]
        if ":pending" in ExpressionAttributeValues:
            if item["status"] != "quarantined":
                raise ConditionalCheckFailedException()
            item.update(
                {
                    "status": ExpressionAttributeValues[":pending"],
                    "redrive_effect_id": ExpressionAttributeValues[":effect_id"],
                    "redrive_binding_sha256": ExpressionAttributeValues[":binding_sha256"],
                    "redrive_started_at": ExpressionAttributeValues[":started_at"],
                }
            )
        else:
            if item["status"] != "redrive_pending":
                raise ConditionalCheckFailedException()
            item["status"] = ExpressionAttributeValues[":redriven"]
            item.setdefault("redriven_at", ExpressionAttributeValues[":at"])
            item.setdefault("redriven_run_id", ExpressionAttributeValues[":run_id"])
        return {"Attributes": dict(item)}


class QueryingQuarantineTable(InMemoryQuarantineTable):
    def __init__(self, pages: dict[str, list[list[dict[str, Any]]]]) -> None:
        super().__init__()
        self.pages = {key: list(value) for key, value in pages.items()}

    def query(self, **kwargs):
        status = _condition_literal(kwargs["KeyConditionExpression"])
        pages = self.pages[status]
        page = pages.pop(0)
        response = {"Items": page}
        if pages:
            response["LastEvaluatedKey"] = {"quarantine_id": "next"}
        return response


def _condition_literal(condition: Any) -> str:
    pending = [condition]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and value in {"quarantined", "redrive_pending"}:
            return value
        pending.extend(getattr(value, "_values", ()))
    raise AssertionError("status condition was not recognized")
