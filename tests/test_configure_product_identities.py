"""Provisioning contract for Cognito identities and PII-free role mappings."""

import hashlib

from botocore.exceptions import ClientError

from scripts.configure_product_identities import (
    DesiredIdentity,
    persist_principal_mappings,
    provision_cognito_identities,
)


class FakeCognito:
    def __init__(self):
        self.users = {
            "existing-viewer": {
                "Username": "existing-viewer",
                "UserAttributes": [{"Name": "sub", "Value": "viewer-sub"}],
            }
        }
        self.groups = {"existing-viewer": {"operator"}}
        self.calls = []

    def admin_get_user(self, **kwargs):
        self.calls.append(("get", kwargs))
        user = self.users.get(kwargs["Username"])
        if user is None:
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": "absent"}},
                "AdminGetUser",
            )
        return user

    def admin_create_user(self, **kwargs):
        self.calls.append(("create", kwargs))
        username = kwargs["Username"]
        user = {
            "Username": username,
            "Attributes": [{"Name": "sub", "Value": "operator-sub"}],
        }
        self.users[username] = user
        return {"User": user}

    def admin_set_user_password(self, **kwargs):
        self.calls.append(("password", kwargs))

    def admin_list_groups_for_user(self, **kwargs):
        self.calls.append(("list-groups", kwargs))
        return {
            "Groups": [
                {"GroupName": group}
                for group in sorted(self.groups.get(kwargs["Username"], set()))
            ]
        }

    def admin_remove_user_from_group(self, **kwargs):
        self.calls.append(("remove-group", kwargs))
        self.groups.setdefault(kwargs["Username"], set()).discard(kwargs["GroupName"])

    def admin_add_user_to_group(self, **kwargs):
        self.calls.append(("add-group", kwargs))
        self.groups.setdefault(kwargs["Username"], set()).add(kwargs["GroupName"])


def test_provisioning_creates_or_updates_users_and_returns_only_hashes():
    client = FakeCognito()
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool"

    mappings = provision_cognito_identities(
        client,
        user_pool_id="us-east-1_pool",
        issuer=issuer,
        desired=(
            DesiredIdentity("existing-viewer", "Viewer-password-123!", "viewer"),
            DesiredIdentity("new-operator", "Operator-password-123!", "operator"),
        ),
    )

    assert mappings == (
        (
            hashlib.sha256(f"{issuer}\0viewer-sub".encode()).hexdigest(),
            hashlib.sha256(f"{issuer}\0managed-role\0viewer".encode()).hexdigest(),
            "viewer",
        ),
        (
            hashlib.sha256(f"{issuer}\0operator-sub".encode()).hexdigest(),
            hashlib.sha256(f"{issuer}\0managed-role\0operator".encode()).hexdigest(),
            "operator",
        ),
    )
    assert client.groups == {
        "existing-viewer": {"viewer"},
        "new-operator": {"operator"},
    }
    create = next(values for name, values in client.calls if name == "create")
    assert create["MessageAction"] == "SUPPRESS"
    assert all(
        values["Permanent"] is True
        for name, values in client.calls
        if name == "password"
    )


def test_mapping_upsert_contains_no_cognito_subject_or_username(monkeypatch):
    executions = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection(Transaction):
        def __init__(self):
            self.transaction = Transaction

        def execute(self, statement, params):
            executions.append((statement, params))

    connection = Connection()
    connects = []
    monkeypatch.setattr(
        "scripts.configure_product_identities.psycopg.connect",
        lambda *args, **kwargs: connects.append((args, kwargs)) or connection,
    )
    principal_hash = "a" * 64
    provisioning_key = "b" * 64

    persist_principal_mappings(
        database_url="postgresql://deploy.example.test/defaultdb?sslmode=verify-full",
        tenant_id="00000000-0000-0000-0000-000000000003",
        mappings=((principal_hash, provisioning_key, "operator"),),
    )

    assert connects[0][1]["application_name"] == "hindsight-product-identity-provisioner"
    assert executions[0][1] == (
        principal_hash,
        provisioning_key,
        "00000000-0000-0000-0000-000000000003",
        "operator",
    )
    assert "principal_hash" in executions[0][0]
    assert "ON CONFLICT (provisioning_key)" in executions[0][0]
    assert "subject" not in executions[0][0].lower()
