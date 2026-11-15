"""Construct C1/C2/C3 JSON Merge PATCH bodies offline; never invoke Azure."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API_VERSION = "2025-01-01"
SECRET = "shared-gateway-activation-manifest"
ROOT = Path(__file__).resolve().parents[1]


def build_payload(
    app: dict[str, Any],
    revision: dict[str, Any],
    *,
    resource_id: str,
    stage: str,
    suffix: str = "",
    deployments: str = "",
    manifest: str = "",
    api_image: str = "",
    existing_revisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require a quiescent R0 snapshot, then pin it in the same request."""
    if stage not in ("C1", "C2", "C3"):
        raise ValueError("stage must be C1, C2 or C3")
    if (
        re.fullmatch(
            r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+"
            r"/providers/Microsoft\.App/containerApps/[a-z0-9-]+",
            resource_id,
        ) is None
        or app.get("id") != resource_id
    ):
        raise ValueError("snapshot must match the explicit Container App resource ID")
    props = app["properties"]
    config = props["configuration"]
    rev = revision["properties"]
    r0 = revision["name"]
    if (
        revision.get("id") != resource_id + "/revisions/" + r0
        or props.get("provisioningState") != "Succeeded"
        or props.get("latestRevisionName") != r0
        or props.get("latestReadyRevisionName") != r0
        or rev.get("active") is not True
        or rev.get("healthState") != "Healthy"
        or rev.get("runningState") != "Running"
        or props["template"] != rev["template"]
    ):
        raise ValueError("R0 must be healthy, active, latest/ready, with an identical template")
    mode = "Single" if stage == "C1" else "Multiple"
    if config.get("activeRevisionsMode") != mode:
        raise ValueError(f"{stage} requires {mode} mode")
    traffic = config["ingress"]["traffic"]
    if len(traffic) != 1 or traffic[0].get("weight") != 100:
        raise ValueError("require a single 100% serving rule without labels")
    rule = traffic[0]
    named = rule == {"revisionName": r0, "weight": 100}
    latest = rule == {"latestRevision": True, "weight": 100}
    if not named and not (stage == "C1" and latest):
        raise ValueError("traffic must identify R0; C3 forbids latestRevision and labels")
    containers = rev["template"]["containers"]
    if len(containers) != 1:
        raise ValueError("require exactly one API container")
    if re.fullmatch(r".+@sha256:[0-9a-f]{64}", containers[0]["image"]) is None:
        raise ValueError("R0 image must be digest-pinned")
    payload: dict[str, Any] = {
        "location": app["location"],
        "properties": {
            "configuration": {
                "activeRevisionsMode": "Multiple",
                "ingress": {"traffic": [{"revisionName": r0, "weight": 100}]},
            },
        },
    }
    if stage == "C1":
        if suffix or deployments or manifest or api_image or existing_revisions is not None:
            raise ValueError("C1 accepts no candidate inputs")
        return payload
    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", suffix) is None:
        raise ValueError("candidate suffix must be a lowercase revision suffix")
    if len(app["name"] + "--" + suffix) > 64 or r0 == app["name"] + "--" + suffix:
        raise ValueError("candidate revision name must be new and at most 64 characters")
    template = copy.deepcopy(rev["template"])
    env = template["containers"][0].get("env", [])
    by_name = {item["name"]: item for item in env}
    if len(by_name) != len(env):
        raise ValueError("duplicate environment variable")

    def require_disabled(name: str) -> None:
        item = by_name.get(name)
        if item is not None and (set(item) != {"name", "value"} or item["value"] != "false"):
            raise ValueError(f"{name} must be absent or a literal false value")

    template["revisionSuffix"] = suffix
    if stage == "C2":
        if deployments or manifest or not api_image or existing_revisions is None:
            raise ValueError("C2 requires an API image and fresh, complete revision inventory evidence only")
        require_disabled("AI4IA_CHAT_PILOT_ISOLATION_ENABLED")
        require_disabled("AI4IA_SHARED_GATEWAY_ENABLED")
        if (
            not isinstance(existing_revisions, dict)
            or set(existing_revisions) != {"complete", "names", "observedAt", "pages", "source"}
            or existing_revisions.get("complete") is not True
            or existing_revisions.get("source") != "Microsoft.App/containerApps/revisions"
            or type(existing_revisions.get("pages")) is not int
            or not 1 <= existing_revisions["pages"] <= 31
        ):
            raise ValueError("revision inventory must carry complete bounded ARM pagination evidence")
        observed_at = existing_revisions["observedAt"]
        if not isinstance(observed_at, str):
            raise ValueError("revision inventory observation time is invalid")
        try:
            observed = datetime.fromisoformat(observed_at)
        except ValueError as exc:
            raise ValueError("revision inventory observation time is invalid") from exc
        now = datetime.now(UTC)
        age_seconds = (now - observed.astimezone(UTC)).total_seconds() if observed.tzinfo else -1
        if age_seconds < -30 or age_seconds > 120:
            raise ValueError("revision inventory must be observed within the last 120 seconds")
        revision_names = existing_revisions["names"]
        if (
            not isinstance(revision_names, list)
            or not 1 <= len(revision_names) <= 640
            or any(
                not isinstance(name, str)
                or re.fullmatch(re.escape(app["name"]) + r"--[a-z0-9-]{1,80}", name) is None
                for name in revision_names
            )
            or len(set(revision_names)) != len(revision_names)
            or r0 not in revision_names
        ):
            raise ValueError("existing revision-name inventory is incomplete or ambiguous")
        candidate_name = app["name"] + "--" + suffix
        if candidate_name in revision_names:
            raise ValueError("candidate revision name already exists")
        acr_image = re.compile(
            r"(?P<repository>[a-z0-9]+\.azurecr\.io/ai4ia/api-[a-z0-9]+(?:-[a-z0-9]+)*)"
            r"@sha256:[0-9a-f]{64}"
        )
        old_image = containers[0]["image"]
        old_match = acr_image.fullmatch(old_image)
        new_match = acr_image.fullmatch(api_image)
        if (
            old_match is None
            or new_match is None
            or old_match["repository"] != new_match["repository"]
            or old_image == api_image
        ):
            raise ValueError("candidate must be a new digest-pinned image in R0's exact API repository")
        template["containers"][0]["image"] = api_image
        payload["properties"]["template"] = template
        return payload
    if api_image or existing_revisions is not None:
        raise ValueError("C3 accepts no image-stage inputs")
    if SECRET not in {secret["name"] for secret in config.get("secrets", [])}:
        raise ValueError("manifest secret must already exist (values are never emitted)")
    require_disabled("AI4IA_CHAT_PILOT_ISOLATION_ENABLED")
    require_disabled("AI4IA_SHARED_GATEWAY_ENABLED")
    names = [name.strip() for name in deployments.split(",")]
    if not all(names) or len(set(names)) != len(names):
        raise ValueError("opt-ins must be nonempty and unique")
    sys.path.insert(0, str(ROOT / "app" / "api" / "src"))
    from ai4ia_api.shared_gateway_contract import load_shared_gateway_manifest

    contract = load_shared_gateway_manifest(
        manifest, api_identity_client_id=by_name.get("AZURE_CLIENT_ID", {}).get("value"),
    )
    if (
        contract.contract_version != "2.2.0"
        or contract.status != "resolved"
        or contract.unresolved_opt_ins(names)
    ):
        raise ValueError("require a resolved 2.2.0 manifest and exact resolvable opt-ins")
    updates = {
        "AI4IA_SHARED_GATEWAY_ENABLED": {"value": "true"},
        "AI4IA_SHARED_GATEWAY_DEPLOYMENTS": {"value": ",".join(names)},
        "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST": {"secretRef": SECRET},
    }
    candidate_env = [item for item in env if item["name"] not in updates]
    candidate_env.extend({"name": name, **value} for name, value in updates.items())
    template["containers"][0]["env"] = candidate_env
    payload["properties"]["template"] = template
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--revision", type=Path, required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--stage", choices=("C1", "C2", "C3"), required=True)
    parser.add_argument("--suffix", default="")
    parser.add_argument("--deployments", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--api-image", default="")
    parser.add_argument(
        "--existing-revisions", type=Path,
        help="Fresh complete ARM pagination evidence JSON (max age 120 seconds)",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = build_payload(
            json.loads(args.app.read_text(encoding="utf-8-sig")),
            json.loads(args.revision.read_text(encoding="utf-8-sig")),
            resource_id=args.resource_id,
            stage=args.stage,
            suffix=args.suffix,
            deployments=args.deployments.read_text().strip() if args.deployments else "",
            manifest=args.manifest.read_text() if args.manifest else "",
            api_image=args.api_image,
            existing_revisions=(
                json.loads(args.existing_revisions.read_text(encoding="utf-8-sig"))
                if args.existing_revisions else None
            ),
        )
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(payload, output, indent=2)
            output.write("\n")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Cutover payload refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
