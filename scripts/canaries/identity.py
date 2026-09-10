"""Exchange only the current workflow's OIDC assertion for the canary API audience."""

from __future__ import annotations

import base64
import re
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .configuration import Configuration
from .contracts import CanaryError, Run, WORKFLOW, integer, obj, strict_json
from .transport import Transport

EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"


def claims(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or len(token) > 16_384 or not re.fullmatch(
        r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token
    ):
        raise CanaryError("identity_rejected")
    header, body, _ = token.split(".")
    try:
        decoded_header = obj(strict_json(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))))
        decoded = obj(strict_json(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))
    except (ValueError, CanaryError) as exc:
        raise CanaryError("identity_rejected") from exc
    if decoded_header.get("alg") != "RS256":
        raise CanaryError("identity_rejected")
    return decoded


def _lifetime(data: dict[str, Any], now: datetime) -> None:
    current = int(now.timestamp())
    expires = integer(data.get("exp"), current + 150, current + 7200)
    issued = integer(data.get("iat"), current - 3600, current + 30)
    not_before = integer(data.get("nbf", issued), current - 3600, current + 30)
    if not_before >= expires:
        raise CanaryError("identity_rejected")


def validate_api_token(token: str, config: Configuration, now: datetime) -> None:
    data = claims(token)
    _lifetime(data, now)
    audience = config.audience.removeprefix("api://")
    application = data.get("azp", data.get("appid"))
    if (
        data.get("iss") != f"https://login.microsoftonline.com/{config.tenant_id}/v2.0"
        or data.get("tid") != config.tenant_id
        or data.get("aud") not in (audience, f"api://{audience}")
        or application != config.client_id or data.get("oid") != config.object_id
        or (data.get("appid") is not None and data["appid"] != config.client_id)
        or data.get("idtyp", "app") != "app"
        or data.get("roles", []) != [] or data.get("scp", "") != ""
        or any(key in data for key in ("preferred_username", "email", "unique_name"))
    ):
        raise CanaryError("identity_rejected")
    # These are additional scope checks, NOT local signature validation. Entra's
    # HTTPS token endpoint issued the token; the API independently validates its
    # signature, issuer, tenant, audience, and lifetime before any owned read.


def oidc_url(env: Mapping[str, str]) -> str:
    value = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    try:
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise CanaryError("invalid_configuration") from exc
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https" or parsed.netloc != host
        or not host.endswith(".actions.githubusercontent.com")
        or not parsed.path.startswith("/") or parsed.fragment
        or re.search(r"[\x00-\x20\x7f\\]", value) or len(value) > 4096
    ):
        raise CanaryError("invalid_configuration")
    if len(query) > 8 or any(key == "audience" for key, _ in query):
        raise CanaryError("invalid_configuration")
    return urlunsplit(parsed._replace(query=urlencode([*query, ("audience", EXCHANGE_AUDIENCE)])))


async def acquire(
    config: Configuration, run: Run, env: Mapping[str, str], now: datetime,
    *, transport: Transport | None = None,
) -> str:
    url = oidc_url(env)
    runner_token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not runner_token or len(runner_token) > 16_384 or re.search(r"\s", runner_token):
        raise CanaryError("identity_rejected")
    if transport is None:
        async with Transport({
            f"https://{urlsplit(url).netloc}", "https://login.microsoftonline.com",
        }) as client:
            return await acquire(config, run, env, now, transport=client)
    result = await transport.request("GET", url, token=runner_token, limit=24 * 1024)
    if result.status != 200:
        raise CanaryError("auth_rejected")
    assertion = result.object().get("value")
    if not isinstance(assertion, str):
        raise CanaryError("identity_rejected")
    data = claims(assertion)
    _lifetime(data, now)
    if any(data.get(key) != expected for key, expected in {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": EXCHANGE_AUDIENCE,
        "sub": f"repo:{run.repository}:ref:refs/heads/main",
        "repository": run.repository,
        "repository_id": str(run.repository_id),
        "ref": "refs/heads/main",
        "workflow_ref": f"{run.repository}/{WORKFLOW}@refs/heads/main",
        "run_id": str(run.run_id),
        "run_attempt": "1",
        "sha": run.sha,
    }.items()):
        raise CanaryError("identity_rejected")
    token_response = await transport.request(
        "POST", f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token",
        body=urlencode({
            "client_id": config.client_id,
            "scope": f"{config.audience}/.default",
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        }).encode("ascii"),
        content_type="application/x-www-form-urlencoded", limit=24 * 1024,
    )
    if token_response.status != 200:
        raise CanaryError("auth_rejected")
    response = token_response.object()
    token = response.get("access_token")
    token_type = response.get("token_type")
    if not isinstance(token, str) or not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise CanaryError("identity_rejected")
    validate_api_token(token, config, now)
    return token
