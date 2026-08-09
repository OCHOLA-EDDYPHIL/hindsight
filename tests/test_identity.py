"""Cognito claim validation and opaque principal mapping tests."""

import hashlib

import pytest

from hindsight.identity import (
    IdentityForbidden,
    IdentityUnauthenticated,
    PrincipalMapping,
    resolve_product_identity,
    verify_gateway_claims,
)


ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
CLIENT_ID = "public-spa-client"
TENANT_ID = "00000000-0000-0000-0000-000000000003"


def _claims(**overrides):
    return {
        "iss": ISSUER,
        "client_id": CLIENT_ID,
        "token_use": "access",
        "sub": "cognito-subject",
        "exp": "200",
        "cognito:groups": "[\"operator\"]",
        **overrides,
    }


def _scope(claims=None):
    return {
        "aws.event": {
            "requestContext": {
                "authorizer": {"jwt": {"claims": claims or _claims()}}
            }
        }
    }


def test_verified_gateway_claims_are_hashed_without_exposing_subject():
    token = verify_gateway_claims(
        _claims(),
        expected_issuer=ISSUER,
        expected_client_id=CLIENT_ID,
        now=100,
    )

    assert token.principal_hash == hashlib.sha256(
        f"{ISSUER}\0cognito-subject".encode()
    ).hexdigest()
    assert token.role == "operator"
    assert "cognito-subject" not in repr(token)


@pytest.mark.parametrize(
    "claims",
    [
        _claims(iss="https://issuer.invalid"),
        _claims(client_id="another-client"),
        _claims(token_use="id"),
        _claims(exp="100"),
        _claims(sub=""),
    ],
)
def test_missing_mismatched_or_expired_claims_fail_authentication(claims):
    with pytest.raises(IdentityUnauthenticated):
        verify_gateway_claims(
            claims,
            expected_issuer=ISSUER,
            expected_client_id=CLIENT_ID,
            now=100,
        )


@pytest.mark.parametrize(
    "groups",
    [
        None,
        "",
        "viewer operator",
        '["viewer", "operator"]',
        '["viewer", "viewer"]',
        '["administrator"]',
    ],
)
def test_missing_conflicting_or_unknown_groups_fail_closed(groups):
    with pytest.raises(IdentityForbidden):
        verify_gateway_claims(
            _claims(**{"cognito:groups": groups}),
            expected_issuer=ISSUER,
            expected_client_id=CLIENT_ID,
            now=100,
        )


def test_identity_uses_verified_scope_and_intersects_token_and_database_roles():
    observed_hashes = []

    def mapping_lookup(principal_hash, _db_url):
        observed_hashes.append(principal_hash)
        return PrincipalMapping(
            id="00000000-0000-0000-0000-000000000099",
            tenant_id=TENANT_ID,
            role="viewer",
            status="active",
        )

    identity = resolve_product_identity(
        _scope(),
        db_url="postgresql://unused",
        expected_issuer=ISSUER,
        expected_client_id=CLIENT_ID,
        now=100,
        mapping_lookup=mapping_lookup,
        tenant_lookup=lambda tenant_id, _db_url: (
            "acceptance" if tenant_id == TENANT_ID else "wrong",
            "active",
        ),
    )

    assert len(observed_hashes) == 1
    assert identity.token_role == "operator"
    assert identity.mapped_role == "viewer"
    assert identity.effective_role == "viewer"
    assert identity.scopes == frozenset({"read", "realtime"})
    assert identity.tenant_id == TENANT_ID
    assert "sub" not in identity.public_payload()


@pytest.mark.parametrize("mapping_status", ["revoked", "missing"])
def test_inactive_or_missing_mappings_fail_closed(mapping_status):
    mapping = None
    if mapping_status != "missing":
        mapping = PrincipalMapping(
            id="principal-id",
            tenant_id=TENANT_ID,
            role="operator",
            status=mapping_status,
        )
    with pytest.raises(IdentityForbidden):
        resolve_product_identity(
            _scope(),
            db_url="postgresql://unused",
            expected_issuer=ISSUER,
            expected_client_id=CLIENT_ID,
            now=100,
            mapping_lookup=lambda *_args: mapping,
            tenant_lookup=lambda *_args: ("acceptance", "active"),
        )


def test_archived_tenant_and_spoofed_authorization_header_do_not_authorize():
    mapping = PrincipalMapping(
        id="principal-id",
        tenant_id=TENANT_ID,
        role="operator",
        status="active",
    )
    with pytest.raises(IdentityForbidden):
        resolve_product_identity(
            _scope(),
            db_url="postgresql://unused",
            expected_issuer=ISSUER,
            expected_client_id=CLIENT_ID,
            now=100,
            mapping_lookup=lambda *_args: mapping,
            tenant_lookup=lambda *_args: ("acceptance", "archived"),
        )
    with pytest.raises(IdentityUnauthenticated):
        resolve_product_identity(
            {"headers": {"authorization": "Bearer spoofed"}},
            db_url="postgresql://unused",
            expected_issuer=ISSUER,
            expected_client_id=CLIENT_ID,
            now=100,
            mapping_lookup=lambda *_args: mapping,
            tenant_lookup=lambda *_args: ("acceptance", "active"),
        )
