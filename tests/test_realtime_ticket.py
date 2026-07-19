import pytest


def test_realtime_ticket_is_tenant_bound_tamper_evident_and_expiring():
    from hindsight.realtime_ticket import issue_realtime_ticket, verify_realtime_ticket

    ticket = issue_realtime_ticket(
        tenant_id="00000000-0000-0000-0000-000000000002",
        secret="secret",
        now=100,
        ttl_seconds=60,
    )

    assert (
        verify_realtime_ticket(ticket, secret="secret", now=159)
        == "00000000-0000-0000-0000-000000000002"
    )
    with pytest.raises(ValueError, match="invalid"):
        verify_realtime_ticket(ticket + "tampered", secret="secret", now=159)
    with pytest.raises(ValueError, match="expired"):
        verify_realtime_ticket(ticket, secret="secret", now=160)


def test_realtime_ticket_lifetime_is_bounded():
    from hindsight.realtime_ticket import issue_realtime_ticket

    with pytest.raises(ValueError, match="out of bounds"):
        issue_realtime_ticket(tenant_id="tenant", secret="secret", ttl_seconds=301)


def test_realtime_ticket_preserves_epoch_time_and_requires_uuid_tenant():
    from hindsight.realtime_ticket import issue_realtime_ticket, verify_realtime_ticket

    tenant_id = "00000000-0000-0000-0000-000000000002"
    ticket = issue_realtime_ticket(
        tenant_id=tenant_id,
        secret="secret",
        ttl_seconds=1,
        now=0,
    )

    assert verify_realtime_ticket(ticket, secret="secret", now=0) == tenant_id
    with pytest.raises(ValueError, match="tenant_id must be a UUID"):
        issue_realtime_ticket(tenant_id="tenant", secret="secret")
