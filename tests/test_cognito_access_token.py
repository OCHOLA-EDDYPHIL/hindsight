"""Privileged acceptance-token helper tests."""

import pytest

from scripts.cognito_access_token import admin_access_token


def test_admin_access_token_uses_iam_only_auth_flow():
    calls = []

    class Client:
        def admin_initiate_auth(self, **kwargs):
            calls.append(kwargs)
            return {"AuthenticationResult": {"AccessToken": "short-lived-access-token"}}

    token = admin_access_token(
        Client(),
        user_pool_id="us-east-1_pool",
        client_id="public-client",
        username="acceptance-operator",
        password="Password-123!",
    )

    assert token == "short-lived-access-token"
    assert calls == [
        {
            "UserPoolId": "us-east-1_pool",
            "ClientId": "public-client",
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": {
                "USERNAME": "acceptance-operator",
                "PASSWORD": "Password-123!",
            },
        }
    ]


def test_admin_access_token_rejects_challenge_or_missing_token():
    class ChallengeClient:
        def admin_initiate_auth(self, **_kwargs):
            return {"ChallengeName": "NEW_PASSWORD_REQUIRED"}

    class EmptyClient:
        def admin_initiate_auth(self, **_kwargs):
            return {"AuthenticationResult": {}}

    with pytest.raises(RuntimeError, match="unsupported challenge"):
        admin_access_token(
            ChallengeClient(),
            user_pool_id="pool",
            client_id="client",
            username="operator",
            password="password",
        )
    with pytest.raises(RuntimeError, match="did not return"):
        admin_access_token(
            EmptyClient(),
            user_pool_id="pool",
            client_id="client",
            username="operator",
            password="password",
        )
