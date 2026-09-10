"""Operator-approved, finite activation scope, independent of deploy credentials."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Mapping

from .contracts import (
    CanaryError, INTERVAL_SECONDS, MAX_RUNS, MAX_SECONDS, Run, WORKFLOW,
    digest, integer, obj, public_origin, strict_json, timestamp,
)

_GUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


def guid(value: object) -> str:
    if not isinstance(value, str) or not _GUID.fullmatch(value) or value == "0" * 8 + "-0000-0000-0000-000000000000":
        raise CanaryError("invalid_configuration")
    return value


def current_run(env: Mapping[str, str]) -> Run:
    try:
        result = Run(
            repository=env["GITHUB_REPOSITORY"],
            repository_id=int(env["GITHUB_REPOSITORY_ID"]),
            run_id=int(env["GITHUB_RUN_ID"]),
            number=int(env["GITHUB_RUN_NUMBER"]),
            attempt=int(env["GITHUB_RUN_ATTEMPT"]),
            sha=env["GITHUB_SHA"],
        )
    except (KeyError, ValueError) as exc:
        raise CanaryError("invalid_configuration") from exc
    result.validate()
    if (
        env.get("GITHUB_REF") != "refs/heads/main"
        or env.get("GITHUB_EVENT_NAME") not in ("schedule", "workflow_dispatch")
        or env.get("GITHUB_WORKFLOW_REF") != f"{result.repository}/{WORKFLOW}@refs/heads/main"
        or env.get("GITHUB_SERVER_URL") != "https://github.com"
        or env.get("GITHUB_API_URL") != "https://api.github.com"
    ):
        raise CanaryError("invalid_configuration")
    return result


@dataclass(frozen=True)
class RealtimeActor:
    client_id: str
    object_id: str


@dataclass(frozen=True)
class Configuration:
    tenant_id: str
    client_id: str
    object_id: str
    audience: str
    web_origin: str
    api_origin: str
    approval_id: str
    expires_at: str
    approved_runs: int
    interval_seconds: int
    acknowledge_no_hard_bill_cap: bool
    actor_ready: bool
    cleanup_approved: bool
    ga_enabled: bool
    realtime_actor: RealtimeActor | None = None

    @classmethod
    def load(cls, env: Mapping[str, str], now: datetime) -> Configuration | None:
        enabled = env.get("AI4IA_CANARY_ENABLED", "")
        if enabled in ("", "false"):
            return None
        if enabled != "true":
            raise CanaryError("invalid_configuration")
        if env.get("AI4IA_CANARY_HARD_USD_CAP"):
            raise CanaryError("hard_bill_cap_unsupported")
        raw = obj(strict_json(env.get("AI4IA_CANARY_CONFIG", "").encode("utf-8"), limit=4096))
        if set(raw) not in (
            set(cls.__dataclass_fields__), set(cls.__dataclass_fields__) - {"realtime_actor"},
        ):
            raise CanaryError("invalid_configuration")
        raw = raw.copy()
        if raw.get("realtime_actor") is not None:
            actor = obj(raw["realtime_actor"], {"client_id", "object_id"})
            raw["realtime_actor"] = RealtimeActor(guid(actor["client_id"]), guid(actor["object_id"]))
        result = cls(**raw)
        for key in ("tenant_id", "client_id", "object_id", "approval_id"):
            guid(getattr(result, key))
        audience_id = result.audience.removeprefix("api://") if isinstance(result.audience, str) else ""
        guid(audience_id)
        deploy_client = guid(env.get("DEPLOY_CLIENT_ID", "").lower())
        if result.client_id in (deploy_client, audience_id) or result.object_id == result.client_id:
            raise CanaryError("identity_rejected")
        for key in ("web_origin", "api_origin"):
            if public_origin(getattr(result, key)) != getattr(result, key):
                raise CanaryError("invalid_configuration")
        for key in ("acknowledge_no_hard_bill_cap", "actor_ready", "cleanup_approved"):
            if getattr(result, key) is not True:
                raise CanaryError("not_ready")
        if type(result.ga_enabled) is not bool:
            raise CanaryError("invalid_configuration")
        if result.ga_enabled and result.realtime_actor is None:
            raise CanaryError("not_ready")
        if result.realtime_actor is not None and (
            result.realtime_actor.client_id in (result.client_id, deploy_client, audience_id)
            or result.realtime_actor.object_id in (result.object_id, result.realtime_actor.client_id)
        ):
            raise CanaryError("identity_rejected")
        integer(result.approved_runs, 1, MAX_RUNS)
        integer(result.interval_seconds, INTERVAL_SECONDS, INTERVAL_SECONDS)
        expiration = timestamp(result.expires_at)
        if expiration <= now + timedelta(seconds=MAX_SECONDS):
            raise CanaryError("approval_expired")
        if expiration > now + timedelta(days=7):
            raise CanaryError("invalid_configuration")
        return result

    @property
    def scope_digest(self) -> str:
        # Only this opaque binding is persisted. No identity, host, token, or
        # private configuration is copied into workflow artifacts.
        return digest(asdict(self))

    @property
    def approval_digest(self) -> str:
        return digest(self.approval_id)

    def for_realtime(self) -> Configuration:
        if not self.ga_enabled or self.realtime_actor is None:
            raise CanaryError("not_ready")
        return replace(
            self, client_id=self.realtime_actor.client_id, object_id=self.realtime_actor.object_id,
            ga_enabled=False, realtime_actor=None,
        )
