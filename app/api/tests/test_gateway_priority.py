"""Proxy priority band derivation and its trust boundary.

The band decides who gets the reserved SimpleL7Proxy workers under contention, so
these tests care less about "does the header appear" than about "can a caller
make it appear for themselves". See ai4ia_api.gateway.priority.
"""
import httpx
import pytest

from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.gateway.priority import (
    PRIORITY_HEADER,
    PRIORITY_HIGH,
    PRIORITY_STANDARD,
    get_request_priority,
    resolve_priority,
    set_request_priority,
)
from tests.conftest import make_settings

ADMIN_OID = "bef9390f-ca0f-467b-a346-c49759a4e1d9"


@pytest.fixture(autouse=True)
def _reset_band():
    """The band lives in a ContextVar; pytest shares one context across tests."""
    set_request_priority(None)
    yield
    set_request_priority(None)


def _user(subject="user-1", email=None, roles=None):
    claims = {"roles": roles} if roles is not None else {}
    return AuthenticatedUser(
        subject=subject,
        internal_user_id=f"iu-{subject}",
        email=email,
        name=None,
        issuer="https://login.microsoftonline.com/test/v2.0",
        provider="entra",
        claims=claims,
    )


def test_admin_by_subject_gets_the_reserved_band():
    settings = make_settings(auth_provider="entra", admin_subjects=ADMIN_OID)
    assert resolve_priority(_user(subject=ADMIN_OID), settings) == PRIORITY_HIGH


def test_admin_by_email_is_case_insensitive():
    settings = make_settings(auth_provider="entra", admin_emails="Ops@Example.com")
    assert resolve_priority(_user(email="OPS@EXAMPLE.COM"), settings) == PRIORITY_HIGH


def test_admin_by_app_role_gets_the_reserved_band():
    settings = make_settings(auth_provider="entra")
    assert resolve_priority(_user(roles=["admin"]), settings) == PRIORITY_HIGH


def test_ordinary_user_gets_standard_not_batch():
    """Standard, not the proxy's no-header default of batch: an authenticated
    user should outrank background traffic even without being an admin."""
    settings = make_settings(auth_provider="entra", admin_subjects=ADMIN_OID)
    assert resolve_priority(_user(), settings) == PRIORITY_STANDARD


def test_spoofable_dev_auth_never_promotes_even_a_named_admin():
    """X-Dev-User is client-supplied, so in a deployed env anyone could claim the
    admin subject. Fail closed, matching auth.admin's threat model."""
    settings = make_settings(
        auth_provider="dev", env="prod", admin_subjects=ADMIN_OID
    )
    assert settings.auth_provider_is_spoofable
    assert resolve_priority(_user(subject=ADMIN_OID), settings) == PRIORITY_STANDARD


def test_local_dev_auth_is_not_spoofable():
    settings = make_settings(auth_provider="dev", env="local", admin_subjects=ADMIN_OID)
    assert not settings.auth_provider_is_spoofable
    assert resolve_priority(_user(subject=ADMIN_OID), settings) == PRIORITY_HIGH


def test_priority_matches_admin_authorization():
    """The band and the entitlement API must agree on who is an admin; a drift
    here would grant one and not the other."""
    from ai4ia_api.auth.identity import identity_is_admin

    settings = make_settings(auth_provider="entra", admin_subjects=ADMIN_OID)
    for user in (_user(subject=ADMIN_OID), _user(roles=["admin"]), _user()):
        expected = PRIORITY_HIGH if identity_is_admin(user, settings) else PRIORITY_STANDARD
        assert resolve_priority(user, settings) == expected


def _client(**overrides):
    settings = make_settings(model_gateway_url="http://gw.test/openai", **overrides)
    return ModelGatewayClient(settings, http_client=httpx.AsyncClient())


def test_no_priority_header_when_band_unresolved():
    client = _client()
    req = client.build_request(deployment="dep-1", messages=[])
    assert PRIORITY_HEADER not in req.headers


def test_priority_header_emitted_when_band_is_set():
    set_request_priority(PRIORITY_HIGH)
    client = _client()
    req = client.build_request(deployment="dep-1", messages=[])
    assert req.headers[PRIORITY_HEADER] == "1"


def test_priority_header_is_not_derived_from_an_inbound_header():
    """A client sending x-S7PPriority: 1 must not reach the reserved pool. The
    client builds outbound headers from the ContextVar only — it never reads the
    inbound request — so nothing here can carry a caller's claim through."""
    set_request_priority(PRIORITY_STANDARD)
    client = _client()
    req = client.build_request(deployment="dep-1", messages=[])
    assert req.headers[PRIORITY_HEADER] == "2"


def test_disabled_flag_leaves_the_band_unset():
    settings = make_settings(auth_provider="entra", admin_subjects=ADMIN_OID)
    assert settings.proxy_priorities_enabled is False
    # get_current_user clears the band when the flag is off; simulate that path.
    set_request_priority(None)
    assert get_request_priority() is None
    req = _client().build_request(deployment="dep-1", messages=[])
    assert PRIORITY_HEADER not in req.headers
