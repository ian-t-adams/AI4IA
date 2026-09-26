"""The avatars policy domain: parse, decide, route inventory and dispatch, paired."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai4ia_api.photo_avatars.availability import policy_state
from ai4ia_api.policy.context import bind_authenticated, clear_policy_context
from ai4ia_api.policy.dispatch import authorize_dispatch
from ai4ia_api.policy.models import PolicyError, PolicyRequest, parse_policy_config
from ai4ia_api.policy.routes import (
    PHOTO_AVATAR_OPERATIONS,
    PHOTO_AVATAR_UNGRANTED,
    authorize_http_operation,
)
from ai4ia_api.routers import photo_avatars as router_module
from tests.test_group_policy import GROUP, service, user

PILOT = {"domains": {"avatars": {
    "default": {"allow": []},
    "mappings": [{"claim": "groups", "value": GROUP, "allow": ["create", "use"]}],
}}}


def test_the_domain_accepts_only_its_two_actions():
    parse_policy_config(json.dumps(PILOT))
    for bad in (["delete"], ["create", "CREATE"], ["*"]):
        with pytest.raises(ValueError):
            parse_policy_config(json.dumps({"domains": {"avatars": {"default": {"allow": bad}}}}))


@pytest.mark.parametrize("groups, allowed", [([GROUP], True), ([], False)])
async def test_a_pilot_group_mapping_grants_creation_and_use_to_members_only(groups, allowed):
    policy, _ = service(PILOT)
    actor = await policy.resolve(user(groups=groups))
    for operation in ("avatar.create", "avatar.use"):
        assert (await policy.authorize(actor, PolicyRequest(operation))).allowed is allowed


async def test_an_absent_domain_keeps_existing_behavior():
    policy, _ = service({"domains": {"documents": {"default": {"allow": ["read"]}}}})
    actor = await policy.resolve(user())
    assert (await policy.authorize(actor, PolicyRequest("avatar.create"))).allowed


async def test_creation_is_consumption_and_a_disabled_account_is_refused():
    policy, store = service(PILOT)
    member = user(groups=[GROUP])
    from ai4ia_api.entitlements.models import Entitlement

    await store.put(Entitlement(id="owner", userId="owner", disabled=True))
    policy.entitlements._cache.clear()
    actor = await policy.resolve(member)
    assert not (await policy.authorize(actor, PolicyRequest("avatar.create"))).allowed
    # Using an existing avatar is not consumption.
    assert (await policy.authorize(actor, PolicyRequest("avatar.use"))).allowed


async def test_restricted_execution_actors_never_create_or_use_avatars():
    config = {
        **PILOT,
        "canaryActor": {"tenantId": "tenant", "subject": "subject"},
        "domains": {**PILOT["domains"], "tools": {"default": {"allow": []}},
                    "documents": {"default": {"allow": []}}},
    }
    policy, _ = service(config)
    actor = await policy.resolve(user(groups=[GROUP]).model_copy(update={
        "internal_user_id": policy.profile_owner("monitor-canary"),
    }))
    for operation in ("avatar.create", "avatar.use"):
        decision = policy.decide(actor, PolicyRequest(operation))
        assert not decision.allowed and decision.reason == "canary_policy_incompatible"


def test_every_photo_avatar_route_is_classified():
    names = {route.endpoint.__name__ for route in router_module.router.routes}
    assert names == set(PHOTO_AVATAR_OPERATIONS) | PHOTO_AVATAR_UNGRANTED
    assert PHOTO_AVATAR_OPERATIONS == {
        "create_photo_avatar": ("avatar.create",),
        "get_photo_avatar_preview": ("avatar.use",),
    }


def _request(name: str):
    endpoint = SimpleNamespace(__module__="ai4ia_api.routers.photo_avatars", __name__=name)
    return SimpleNamespace(scope={"route": SimpleNamespace(endpoint=endpoint)})


@pytest.mark.parametrize("name", sorted(PHOTO_AVATAR_UNGRANTED))
async def test_owner_reads_cleanup_and_reports_need_no_grant(name):
    policy, _ = service(PILOT)
    bind_authenticated(policy, user(groups=[]))
    try:
        await authorize_http_operation(_request(name))  # a non-member still passes
        with pytest.raises(PolicyError):
            await authorize_http_operation(_request("create_photo_avatar"))
    finally:
        clear_policy_context()


async def test_an_unclassified_route_in_the_module_is_refused():
    policy, _ = service(PILOT)
    bind_authenticated(policy, user(groups=[GROUP]))
    try:
        await authorize_http_operation(_request("create_photo_avatar"))
        with pytest.raises(PolicyError) as refused:
            await authorize_http_operation(_request("list_provider_avatars"))
        assert refused.value.decision.reason == "policy_surface_unsupported"
    finally:
        clear_policy_context()


@pytest.mark.parametrize("groups, allowed", [([GROUP], True), ([], False)])
async def test_the_dispatch_seam_rechecks_avatar_creation(groups, allowed):
    policy, _ = service(PILOT)
    bind_authenticated(policy, user(groups=groups))
    try:
        if allowed:
            await authorize_dispatch("avatar", deployment=None, required=True, final=True)
        else:
            with pytest.raises(PolicyError):
                await authorize_dispatch("avatar", deployment=None, required=True, final=True)
    finally:
        clear_policy_context()


async def test_a_zones_restriction_cannot_silently_cover_avatar_dispatch():
    config = {"domains": {**PILOT["domains"], "zones": {"default": {"allow": ["global"]}}}}
    policy, _ = service(config)
    bind_authenticated(policy, user(groups=[GROUP]))
    try:
        with pytest.raises(PolicyError) as refused:
            await authorize_dispatch("avatar", deployment=None, required=True)
        assert refused.value.decision.reason == "policy_surface_unsupported"
    finally:
        clear_policy_context()


@pytest.mark.parametrize("zoned", [True, False])
async def test_availability_advertises_creation_exactly_when_dispatch_would_admit_it(zoned):
    domains = {**PILOT["domains"], **({"zones": {"default": {"allow": ["global"]}}} if zoned else {})}
    policy, _ = service({"domains": domains})
    bind_authenticated(policy, user(groups=[GROUP]))
    try:
        try:
            await authorize_dispatch("avatar", deployment=None, required=True)
            admitted = True
        except PolicyError:
            admitted = False
        assert admitted is not zoned
        assert await policy_state("avatar.create") == ("allowed" if admitted else "unavailable")
        # Using an existing avatar is not model processing scope.
        assert await policy_state("avatar.use") == "allowed"
    finally:
        clear_policy_context()
