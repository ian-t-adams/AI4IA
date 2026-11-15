"""Four fixed permission-metadata GETs with the existing production OIDC identity.

No resource bodies, role assignments, model traffic, pagination, retries or writes.
Permission lists describe reported actions, not successful writes or effective access
in the presence of conditions, deny assignments, policy or linked-scope requirements.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "permission_read_transport", Path(__file__).with_name("hosted_gateway_release.py")
)
assert SPEC is not None and SPEC.loader is not None
TRANSPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRANSPORT)
OIDC = TRANSPORT._oidc()
GATE = "AI4IA_GATEWAY_PERMISSIONS_READ_ENABLED"
GATEWAY_RG = (
    f"/subscriptions/{TRANSPORT.SUBSCRIPTION}"
    "/resourceGroups/rg-planetexpress-gateway-eastus2"
)
DOCEX_RG = "/subscriptions/402263c9-3432-4696-b7bc-4fa55c2c5090/resourceGroups/rg-docex"
SCOPES = (
    ("gateway-resource-group", GATEWAY_RG),
    ("gateway-registry", GATEWAY_RG
     + "/providers/Microsoft.ContainerRegistry/registries/acrai4iapilots7p"),
    ("docex-caller-vnet", DOCEX_RG
     + "/providers/Microsoft.Network/virtualNetworks/vnet-docex-dev-scus-m2hsx2"),
    ("docex-model-account", DOCEX_RG
     + "/providers/Microsoft.CognitiveServices/accounts/cog-docex-dev-eus2-m2hsx2"),
)
PERMISSION_FIELDS = ("actions", "notActions", "dataActions", "notDataActions")
MAX_BODY = 32768
MAX_OUTPUT = 262144


def validate_environment(env: dict[str, str]) -> None:
    expected = {
        GATE: "true",
        "AI4IA_GATEWAY_PERMISSIONS_READ_ADMIT": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": TRANSPORT.REPOSITORY,
        "AZURE_TENANT_ID": TRANSPORT.TENANT,
        "AZURE_SUBSCRIPTION_ID": TRANSPORT.SUBSCRIPTION,
        "AZURE_CLIENT_ID": TRANSPORT.CLIENT,
    }
    sha = env.get("GITHUB_SHA", "")
    if (any(env.get(k) != v for k, v in expected.items())
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or env.get("AI4IA_REVIEWED_SHA") != sha):
        raise TRANSPORT.Refused("admission")
    denied = {
        "http_proxy", "https_proxy", "all_proxy", "no_proxy", "requests_ca_bundle",
        "curl_ca_bundle", "ssl_cert_file", "ssl_cert_dir", "sslkeylogfile",
        "pythonhttpsverify", "azure_authority_host", "azure_regional_authority_name",
    }
    if any(k.lower() in denied and value for k, value in env.items()):
        raise TRANSPORT.Refused("environment")


def permissions(body: bytes) -> list[dict[str, list[str]]]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise TRANSPORT.Refused("response") from None
    if not isinstance(data, dict) or data.get("nextLink"):
        raise TRANSPORT.Refused("incomplete-response")
    rows = data.get("value")
    if not isinstance(rows, list) or len(rows) > 32:
        raise TRANSPORT.Refused("response")
    result = []
    for row in rows:
        if (not isinstance(row, dict) or "actions" not in row or "notActions" not in row
                or set(row) - set(PERMISSION_FIELDS)):
            raise TRANSPORT.Refused("response")
        projected = {}
        for name in PERMISSION_FIELDS:
            values = row.get(name, [])
            if (not isinstance(values, list) or len(values) > 128
                    or any(not isinstance(v, str) or len(v) > 512
                           or not re.fullmatch(r"[A-Za-z0-9_./*-]+", v) for v in values)):
                raise TRANSPORT.Refused("response")
            projected[name] = values
        result.append(projected)
    return result


def execute(env: dict[str, str], *, http=TRANSPORT.http_request, now=time.time) -> dict:
    validate_environment(env)
    timestamp = now()
    receipt = {
        "phase": "credential",
        "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat(),
        "reviewed_sha": env["GITHUB_SHA"],
        "tenant": TRANSPORT.TENANT,
        "client": TRANSPORT.CLIENT,
        "effective_write_access_proven": False,
        "scopes": [],
    }
    try:
        token = TRANSPORT.acquire_arm_token(env, timestamp, http=http)
    except TRANSPORT.Refused as error:
        receipt.update(phase=error.phase, status=error.status)
        return receipt
    for name, scope in SCOPES:
        row = {"name": name, "scope": scope, "status": None,
               "observed": False, "permissions": None}
        receipt["scopes"].append(row)
        try:
            status, _, body = http(
                "GET",
                TRANSPORT.Arm.url(scope + "/providers/Microsoft.Authorization/permissions",
                                  api="2022-04-01"),
                hosts={TRANSPORT.ARM_HOST},
                headers={"Authorization": "Bearer " + token},
                limit=MAX_BODY, timeout=10, phase="permissions",
            )
            row["status"] = status
            if status != 200:
                raise TRANSPORT.Refused("permissions", status)
            row["permissions"] = permissions(body)
            row.update(observed=True, sha256=hashlib.sha256(body).hexdigest())
        except TRANSPORT.Refused as error:
            row["phase"] = error.phase
        except (OSError, HTTPException):
            row["phase"] = "transport"
    receipt["phase"] = "complete" if all(r["observed"] for r in receipt["scopes"]) else "incomplete"
    return receipt


def main() -> int:
    if sys.argv[1:] not in ([], ["--child"]):
        print('{"phase":"admission"}')
        return 1
    try:
        validate_environment(os.environ)
        if sys.argv[1:] == ["--child"]:
            answer = execute(os.environ)
        else:
            code, raw, timed_out, overflow = OIDC._run_owned_child(
                [sys.executable, "-I", "-u", __file__, "--child"],
                timeout=180, max_output=MAX_OUTPUT,
            )
            if timed_out or overflow:
                answer = {"phase": "timeout" if timed_out else "output-overflow"}
            else:
                answer = json.loads(raw)
                if (code not in (0, 1) or not isinstance(answer, dict)
                        or (code == 0) != (answer.get("phase") == "complete")):
                    raise TRANSPORT.Refused("carrier")
    except TRANSPORT.Refused as error:
        answer = {"phase": error.phase, "status": error.status}
    except (OSError, HTTPException, ValueError, RuntimeError):
        answer = {"phase": "carrier"}
    print(json.dumps(answer, separators=(",", ":")))
    return 0 if answer.get("phase") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
