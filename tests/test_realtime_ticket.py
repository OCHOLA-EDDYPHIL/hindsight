import base64
import hashlib
from decimal import Decimal
from types import SimpleNamespace

import pytest


class FakeConditionalCheckFailed(Exception):
    pass


class FakeTicketTable:
    def __init__(self):
        self.items = {}
        self.puts = []
        self.deletes = []
        self.now = 0
        self.meta = SimpleNamespace(
            client=SimpleNamespace(
                exceptions=SimpleNamespace(
                    ConditionalCheckFailedException=FakeConditionalCheckFailed
                )
            )
        )

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        item = dict(kwargs["Item"])
        digest = item["ticket_digest"]
        if digest in self.items:
            raise FakeConditionalCheckFailed()
        self.items[digest] = item
        return {}

    def delete_item(self, **kwargs):
        self.deletes.append(kwargs)
        digest = kwargs["Key"]["ticket_digest"]
        item = self.items.get(digest)
        if (
            item is None
            or item["redeem_before"] <= self.now
            or item["session_expires_at"] <= self.now
        ):
            raise FakeConditionalCheckFailed()
        del self.items[digest]
        returned = {
            key: Decimal(value) if key in {"redeem_before", "session_expires_at"} else value
            for key, value in item.items()
        }
        return {"Attributes": returned}


def test_realtime_ticket_stores_only_digest_and_is_consumed_once():
    from hindsight.realtime_ticket import consume_realtime_ticket, issue_realtime_ticket

    table = FakeTicketTable()
    tenant_id = "00000000-0000-0000-0000-000000000002"
    ticket = issue_realtime_ticket(
        tenant_id=tenant_id,
        access_class="operator",
        principal_id="principal-7",
        session_expires_at=1_000,
        now=100,
        ttl_seconds=60,
        table=table,
    )

    decoded = base64.urlsafe_b64decode(ticket + "=")
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    assert len(decoded) == 32
    assert set(table.items[digest]) == {
        "ticket_digest",
        "tenant_id",
        "access_class",
        "principal_id",
        "redeem_before",
        "session_expires_at",
        "expires_at",
    }
    assert table.items[digest] == {
        "ticket_digest": digest,
        "tenant_id": tenant_id,
        "access_class": "operator",
        "principal_id": "principal-7",
        "redeem_before": 160,
        "session_expires_at": 1_000,
        "expires_at": 160,
    }
    assert ticket not in repr(table.items[digest])
    assert table.puts[0]["ConditionExpression"] is not None

    table.now = 159
    claims = consume_realtime_ticket(ticket, now=159, table=table)

    assert claims.tenant_id == tenant_id
    assert claims.access_class == "operator"
    assert claims.principal_id == "principal-7"
    assert claims.redeem_before == 160
    assert claims.session_expires_at == 1_000
    assert digest not in table.items
    assert table.deletes[0]["ReturnValues"] == "ALL_OLD"
    assert table.deletes[0]["ConditionExpression"] is not None
    with pytest.raises(ValueError, match="invalid or expired"):
        consume_realtime_ticket(ticket, now=159, table=table)


def test_realtime_ticket_redeem_window_is_capped_by_session_expiry():
    from hindsight.realtime_ticket import consume_realtime_ticket, issue_realtime_ticket

    table = FakeTicketTable()
    ticket = issue_realtime_ticket(
        tenant_id="00000000-0000-0000-0000-000000000002",
        access_class="viewer",
        principal_id="principal-8",
        session_expires_at=130,
        now=100,
        ttl_seconds=60,
        table=table,
    )
    item = next(iter(table.items.values()))

    assert item["redeem_before"] == 130
    assert item["expires_at"] == 130
    table.now = 130
    with pytest.raises(ValueError, match="invalid or expired"):
        consume_realtime_ticket(ticket, now=130, table=table)


def test_public_realtime_ticket_has_no_principal_and_rejects_tampering():
    from hindsight.realtime_ticket import consume_realtime_ticket, issue_realtime_ticket

    table = FakeTicketTable()
    ticket = issue_realtime_ticket(
        tenant_id="00000000-0000-0000-0000-000000000002",
        access_class="public",
        session_expires_at=200,
        now=100,
        table=table,
    )

    assert next(iter(table.items.values()))["principal_id"] == ""
    table.now = 101
    replacement = "A" if ticket[-1] != "A" else "B"
    with pytest.raises(ValueError, match="invalid or expired"):
        consume_realtime_ticket(ticket[:-1] + replacement, now=101, table=table)
    claims = consume_realtime_ticket(ticket, now=101, table=table)
    assert claims.access_class == "public"
    assert claims.principal_id is None


@pytest.mark.parametrize("ttl_seconds", [0, 301, True])
def test_realtime_ticket_lifetime_is_bounded(ttl_seconds):
    from hindsight.realtime_ticket import issue_realtime_ticket

    with pytest.raises(ValueError, match="lifetime"):
        issue_realtime_ticket(
            tenant_id="00000000-0000-0000-0000-000000000002",
            access_class="public",
            session_expires_at=1_000,
            ttl_seconds=ttl_seconds,
            now=100,
            table=FakeTicketTable(),
        )


def test_realtime_ticket_validates_tenant_access_and_session_bounds():
    from hindsight.realtime_ticket import issue_realtime_ticket

    table = FakeTicketTable()
    with pytest.raises(ValueError, match="tenant_id must be a UUID"):
        issue_realtime_ticket(
            tenant_id="tenant",
            access_class="public",
            session_expires_at=1_000,
            now=100,
            table=table,
        )
    with pytest.raises(ValueError, match="require a principal"):
        issue_realtime_ticket(
            tenant_id="00000000-0000-0000-0000-000000000002",
            access_class="viewer",
            session_expires_at=1_000,
            now=100,
            table=table,
        )
    with pytest.raises(ValueError, match="cannot bind a principal"):
        issue_realtime_ticket(
            tenant_id="00000000-0000-0000-0000-000000000002",
            access_class="public",
            principal_id="principal-9",
            session_expires_at=1_000,
            now=100,
            table=table,
        )
    with pytest.raises(ValueError, match="already expired"):
        issue_realtime_ticket(
            tenant_id="00000000-0000-0000-0000-000000000002",
            access_class="public",
            session_expires_at=100,
            now=100,
            table=table,
        )
