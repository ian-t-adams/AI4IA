"""Source review controls with real services and no model or directory calls."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.user_agents import UserAgentCreate, UserAgentUpdate
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.auth.policy_claims import verified_policy_claims
from ai4ia_api.main import create_app
from ai4ia_api.policy.context import clear_policy_context
from ai4ia_api.policy.models import PolicyError
from ai4ia_api.publishing.models import (
    ActivationRequest, PublicationError, PublicationSubmit, ReviewRequest,
)
from ai4ia_api.publishing.service import PublicationService
from ai4ia_api.publishing.store import RecordQuery
from ai4ia_api.workflows.record_types import PUBLICATION_VERSION_KIND
from tests.conftest import make_settings


@pytest.fixture
def publication():
    config = {"domains": {"publication": {
        "default": {"allow": ["consume"]},
        "mappings": [
            {"claim": "roles", "value": "Author", "allow": ["submit"]},
            {"claim": "roles", "value": "Reviewer", "allow": ["review"]},
        ],
    }}}
    app = create_app(make_settings(
        auth_provider="entra", entra_tenant_id="tenant", entra_audience="api://test",
        group_policy_enabled=True, asset_publishing_enabled=True,
        group_policy_json=json.dumps(config),
    ))
    with TestClient(app):
        state = app.state
        service = PublicationService(
            state, agents=state.agent_service._store.records,
            workflows=state.workflow_service._store.records,
        )
        yield service
    clear_policy_context()


async def actor(service, name, role):
    return await service.state.policy.resolve(AuthenticatedUser(
        internal_user_id=name, subject=name, issuer="issuer", provider="entra",
        tenant_id="tenant", email=f"{name}@example.com",
        policy_claims=verified_policy_claims({
            "roles": [role] if role else [], "exp": int(time.time()) + 3600,
        }),
    ))


async def submit(service):
    author = await actor(service, "author", "Author")
    source = await service.state.agent_service.create(
        author.owner_id, UserAgentCreate(
            name="shared-helper", systemPrompt="Answer carefully.", tools=["calculator"],
        ), reserved_names=set(),
    )
    model = next(
        model.id for model in service.state.catalog.models
        if model.supportsTools and all(option.modelVersion for option in model.options)
    )
    head = await service.submit(author, "agent", source.name, PublicationSubmit(
        expectedRevision=source.revision,
        audience={"visibility": "shared", "acl": ["consumer@example.com"]},
        modelIds=[model], modes=["chat"], reviewConsent=True,
    ))
    rows = await service._store("agent").query(RecordQuery(
        PUBLICATION_VERSION_KIND, owner_id=author.owner_id,
    ))
    from ai4ia_api.publishing.models import PublicationVersion

    version = PublicationVersion.model_validate(rows[0].body)
    return author, head, version


async def publish(service):
    author, head, version = await submit(service)
    reviewer = await actor(service, "reviewer", "Reviewer")
    reviewed = await service.decide_review(reviewer, ReviewRequest(
        source=version.reference(), expectedHeadRevision=head.revision, decision="approved",
    ))
    active = await service.activate(author, "agent", version.source.name, ActivationRequest(
        source=version.reference(), expectedHeadRevision=reviewed.revision,
    ))
    return author, active, version


async def test_independent_review_owner_activation_consumer_and_withdrawal(publication):
    author, head, version = await publish(publication)
    consumer = await actor(publication, "consumer", "")
    resolved = await publication.resolve_for_execution(consumer, version.reference(), mode="chat")
    assert resolved.version.source.systemPrompt == "Answer carefully."
    assert resolved.approval.reviewerId == "reviewer"
    assert [agent.name for agent in await publication.state.agent_service.list_for("author")] == [
        "shared-helper",
    ]
    assert await publication.state.agent_service.list_for("consumer") == []
    before = version.model_dump(mode="json")
    await publication.withdraw(author, "agent", "shared-helper", head.revision)
    with pytest.raises(PublicationError):
        await publication.resolve_for_execution(consumer, version.reference(), mode="chat")
    _, historical = await publication._version(version.reference())
    assert historical.model_dump(mode="json") == before


async def test_author_role_and_unsolicited_private_review_do_not_grant_access(publication):
    author, head, version = await submit(publication)
    with pytest.raises(PublicationError):
        await publication.review_source(author, version.reference())
    ordinary = await actor(publication, "ordinary", "")
    with pytest.raises(PublicationError):
        await publication.review_source(ordinary, version.reference())
    with pytest.raises(PolicyError):
        await publication.activate(ordinary, "agent", "shared-helper", ActivationRequest(
            source=version.reference(), expectedHeadRevision=head.revision,
        ))
    with pytest.raises(PublicationError):
        await publication.resolve_for_execution(ordinary, version.reference(), mode="chat")
    reviewer = await actor(publication, "reviewer", "Reviewer")
    assert (await publication.review_source(reviewer, version.reference()))[1] == version


async def test_concurrent_reviews_have_one_cas_winner(publication):
    _, head, version = await submit(publication)
    first = await actor(publication, "reviewer-a", "Reviewer")
    second = await actor(publication, "reviewer-b", "Reviewer")
    request = ReviewRequest(
        source=version.reference(), expectedHeadRevision=head.revision, decision="approved",
    )
    results = await asyncio.gather(
        publication.decide_review(first, request), publication.decide_review(second, request),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, PublicationError) for result in results) == 1


async def test_edit_during_review_cannot_approve_changed_source(publication):
    _, head, version = await submit(publication)
    await publication.state.agent_service.update("author", "shared-helper", UserAgentUpdate(
        systemPrompt="Changed after submission.", tools=["calculator"],
        expectedRevision=version.source.revision,
    ))
    reviewer = await actor(publication, "reviewer", "Reviewer")
    with pytest.raises(PublicationError, match="source_changed"):
        await publication.decide_review(reviewer, ReviewRequest(
            source=version.reference(), expectedHeadRevision=head.revision, decision="approved",
        ))


async def test_draft_edits_leave_published_bytes_but_delete_recreate_never_revives(publication):
    _, _, version = await publish(publication)
    consumer = await actor(publication, "consumer", "")
    await publication.state.agent_service.update("author", "shared-helper", UserAgentUpdate(
        systemPrompt="Private new draft.", tools=["calculator"],
    ))
    resolved = await publication.resolve_for_execution(consumer, version.reference(), mode="chat")
    assert resolved.version.source.systemPrompt == "Answer carefully."
    await publication.state.agent_service.delete("author", "shared-helper")
    await publication.state.agent_service.create("author", UserAgentCreate(
        name="shared-helper", systemPrompt="Replacement.", tools=["calculator"],
    ), reserved_names=set())
    with pytest.raises(PublicationError):
        await publication.resolve_for_execution(consumer, version.reference(), mode="chat")
