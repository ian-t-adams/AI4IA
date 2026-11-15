"""Construct offline C1/C2/C3 request bodies; never read Azure or apply a request."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API_VERSION = "2025-01-01"
C3_BLOCKER = (
    "C3 requires the exact offline-classified manifest and its non-activation sidecar"
)
C3_CLASSIFIED_MANIFESTS = {
    "94c2edf581b40a2677f3f90fad76eb2fc957478e5969c5007132d35040e72cb5":
        "nonconfidential-operational",
}
C3_SIDECAR_SHA256 = "f73a55378dbe418e92ca4ea3d0bc8e30ef6ae09778f3aebddc3525e5b571ecc4"
C3_QUALIFIED_LOADER_SHA256 = "cd89beaffe81acb0dd344ddd8a81d1a0c78e890e"
ROOT = Path(__file__).resolve().parents[1]


def validate_revision_rows(
    evidence: object, app_name: str, r0: str, suffix: str, *, now: datetime | None = None,
) -> None:
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"observedAt", "rows"}
    ):
        raise ValueError("revision input must contain only an observation time and reader rows")
    observed_at = evidence["observedAt"]
    if not isinstance(observed_at, str):
        raise TypeError("revision observation time must be a string")
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError as exc:
        raise ValueError("revision observation time is invalid") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("revision observation time must include a timezone offset")
    age_seconds = ((now or datetime.now(UTC)) - observed.astimezone(UTC)).total_seconds()
    if age_seconds < -30 or age_seconds > 120:
        raise ValueError("revision rows must be observed within the last 120 seconds")
    rows = evidence["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 639:
        raise ValueError("revision rows must match the private revision reader's bounded shape")
    names: list[str] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "active", "health", "state"}
            or type(row["active"]) is not bool
            or row["health"] is not None and not isinstance(row["health"], str)
            or row["state"] is not None and not isinstance(row["state"], str)
            or not isinstance(row["name"], str)
            or len(row["name"]) > 64
            or re.fullmatch(re.escape(app_name) + r"--[a-z0-9-]{1,80}", row["name"]) is None
        ):
            raise ValueError("existing revision rows are incomplete or ambiguous")
        names.append(row["name"])
    if len(set(names)) != len(names) or r0 not in names or not any(
        row["name"] == r0 and row["active"] is True for row in rows
    ):
        raise ValueError("existing revision rows are incomplete or ambiguous")
    if app_name + "--" + suffix in names:
        raise ValueError("candidate revision name already exists")


def writable_template_projection(template: dict[str, Any]) -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts._private_recovery_packets import activation_template_projection

    return activation_template_projection(template)


def git_blob_sha1(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def load_c3_contract(
    manifest_bytes: bytes, sidecar_bytes: bytes, api_identity_client_id: str,
) -> tuple[str, Any]:
    """Validate the one offline-classified manifest before parsing or embedding it."""
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if C3_CLASSIFIED_MANIFESTS.get(manifest_digest) != "nonconfidential-operational":
        raise ValueError("C3 manifest does not match the exact offline classification")
    if hashlib.sha256(sidecar_bytes).hexdigest() != C3_SIDECAR_SHA256:
        raise ValueError("C3 evidence sidecar digest does not match the pinned handoff")

    loader_path = ROOT / "app" / "api" / "src" / "ai4ia_api" / "shared_gateway_contract.py"
    if git_blob_sha1(loader_path.read_bytes()) != C3_QUALIFIED_LOADER_SHA256:
        raise ValueError("C3 requires the qualified shared gateway contract loader")

    try:
        raw_manifest = manifest_bytes.decode("utf-8")
        sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("C3 manifest or evidence sidecar JSON is invalid") from exc
    if (
        not isinstance(sidecar, dict)
        or type(sidecar.get("activationReady")) is not bool
        or sidecar["activationReady"] is not False
        or type(sidecar.get("applicationChanged")) is not bool
        or sidecar["applicationChanged"] is not False
        or type(sidecar.get("modelRequests")) is not int
        or sidecar["modelRequests"] != 0
    ):
        raise ValueError("C3 evidence sidecar does not prove the required non-activation state")

    api_source = ROOT / "app" / "api" / "src"
    if str(api_source) not in sys.path:
        sys.path.insert(0, str(api_source))
    from ai4ia_api.shared_gateway_contract import load_shared_gateway_manifest

    try:
        manifest = load_shared_gateway_manifest(
            raw_manifest,
            api_identity_client_id=api_identity_client_id,
            require_api_identity=True,
            pilot_mode=False,
        )
    except ValueError as exc:
        raise ValueError(
            "C3 manifest is rejected by the qualified shared gateway contract loader"
        ) from exc
    if (
        manifest.contract_version != "2.2.0"
        or manifest.status != "resolved"
        or any(capability.enabled for capability in manifest.capabilities.values())
        or sidecar.get("callerClientId") != manifest.caller_client_id
        or sidecar.get("baseUrl") != manifest.base_url
    ):
        raise ValueError("C3 manifest or sidecar binding is not qualified for offline construction")
    return raw_manifest, manifest


def build_payload(
    app: dict[str, Any],
    revision: dict[str, Any],
    *,
    resource_id: str,
    stage: str,
    service: str = "api",
    suffix: str = "",
    api_image: str = "",
    web_image: str = "",
    existing_revisions: dict[str, Any] | None = None,
    manifest_bytes: bytes | None = None,
    classification_sidecar_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Require a quiescent R0 snapshot, then pin it in the same request."""
    if stage not in ("C1", "C2", "C3"):
        raise ValueError("stage must be C1, C2 or C3")
    if (
        service not in ("api", "web")
        or service == "web" and stage == "C3"
        or service == "web" and stage not in ("C1", "C2")
    ):
        raise ValueError("web service supports only C1 and C2 staging")
    if stage != "C3" and (
        manifest_bytes is not None or classification_sidecar_bytes is not None
    ):
        raise ValueError("manifest inputs are accepted only for C3")
    if (
        re.fullmatch(
            r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+"
            r"/providers/Microsoft\.App/containerApps/[a-z0-9-]+",
            resource_id,
        ) is None
        or app.get("id") != resource_id
        or resource_id.rsplit("/", 1)[-1] != app.get("name")
    ):
        raise ValueError("snapshot must match the explicit Container App resource ID")
    if service == "web" and re.fullmatch(
        r"ca-web-[a-z0-9]+(?:-[a-z0-9]+)*", app["name"],
    ) is None:
        raise ValueError("web resource name must identify its exact environment")
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
        or writable_template_projection(props["template"])
        != writable_template_projection(rev["template"])
    ):
        raise ValueError("R0 must be healthy, active, latest/ready, with an identical writable template")
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
    if len(containers) != 1 or containers[0].get("name") != service:
        raise ValueError(f"{stage} requires exactly one {service} container")
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
        if (
            suffix or api_image or web_image or existing_revisions is not None
            or manifest_bytes is not None or classification_sidecar_bytes is not None
        ):
            raise ValueError("C1 accepts no candidate inputs")
        return payload
    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", suffix) is None:
        raise ValueError("candidate suffix must be a lowercase revision suffix")
    if len(app["name"] + "--" + suffix) > 64 or r0 == app["name"] + "--" + suffix:
        raise ValueError("candidate revision name must be new and at most 64 characters")
    template = writable_template_projection(rev["template"])
    env = template["containers"][0].get("env", [])
    by_name = {item["name"]: item for item in env}
    if len(by_name) != len(env):
        raise ValueError("duplicate environment variable")

    def require_disabled(name: str) -> None:
        item = by_name.get(name)
        if item is not None and (set(item) != {"name", "value"} or item["value"] != "false"):
            raise ValueError(f"{name} must be absent or a literal false value")

    template["revisionSuffix"] = suffix
    if stage == "C3":
        if (
            api_image or web_image or existing_revisions is None
            or manifest_bytes is None or classification_sidecar_bytes is None
        ):
            raise ValueError("C3 requires only its exact manifest, sidecar, suffix and revision rows")
        validate_revision_rows(existing_revisions, app["name"], r0, suffix)
        env = template["containers"][0].get("env", [])
        if not isinstance(env, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in env
        ):
            raise ValueError("C3 environment is malformed")
        names = [item["name"] for item in env]
        if len(set(names)) != len(names):
            raise ValueError("duplicate environment variable")
        by_name = {item["name"]: item for item in env}
        identity = by_name.get("AZURE_CLIENT_ID")
        if (
            identity is None or set(identity) != {"name", "value"}
            or not isinstance(identity["value"], str)
        ):
            raise ValueError("C3 requires one literal API managed identity client ID")
        shared_enabled = by_name.get("AI4IA_SHARED_GATEWAY_ENABLED")
        if (
            shared_enabled is None or set(shared_enabled) != {"name", "value"}
            or shared_enabled["value"] != "false"
        ):
            raise ValueError("C3 requires the selector to be a literal false value")
        pilot = by_name.get("AI4IA_CHAT_PILOT_ISOLATION_ENABLED")
        if pilot is not None and (
            set(pilot) != {"name", "value"} or pilot["value"] != "false"
        ):
            raise ValueError("C3 requires chat pilot isolation to remain disabled")
        deployment_opt_in = by_name.get("AI4IA_SHARED_GATEWAY_DEPLOYMENTS")
        if (
            deployment_opt_in is None
            or set(deployment_opt_in) != {"name", "value"}
            or not isinstance(deployment_opt_in["value"], str)
        ):
            raise ValueError("C3 requires an explicit literal deployment opt-in list")
        deployments = tuple(
            item.strip() for item in deployment_opt_in["value"].split(",")
        )
        if (
            not deployments or any(not item for item in deployments)
            or len(set(deployments)) != len(deployments)
        ):
            raise ValueError("C3 deployment opt-in list is empty or ambiguous")
        raw_manifest, manifest = load_c3_contract(
            manifest_bytes, classification_sidecar_bytes, identity["value"],
        )
        if manifest.unresolved_opt_ins(deployments):
            raise ValueError("C3 deployment opt-in list is not resolved by the manifest")
        shared_enabled["value"] = "true"
        manifest_env = by_name.get("AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST")
        if (
            manifest_env is None or set(manifest_env) != {"name", "secretRef"}
            or manifest_env["secretRef"] != "shared-gateway-activation-manifest"
        ):
            raise ValueError("C3 requires the exact existing manifest secret reference")
        manifest_index = names.index("AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST")
        env[manifest_index] = {
            "name": "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST",
            "value": raw_manifest,
        }
        template["containers"][0]["env"] = env
        payload["properties"]["template"] = template
        return payload
    if stage == "C2":
        candidate_image = api_image if service == "api" else web_image
        other_image = web_image if service == "api" else api_image
        if (
            other_image or not candidate_image or existing_revisions is None
        ):
            raise ValueError(
                f"C2 requires a {service} image and revision rows from the private revision reader only"
            )
        if service == "api":
            require_disabled("AI4IA_CHAT_PILOT_ISOLATION_ENABLED")
            require_disabled("AI4IA_SHARED_GATEWAY_ENABLED")
        validate_revision_rows(existing_revisions, app["name"], r0, suffix)
        acr_image = re.compile(
            rf"(?P<registry>[a-z0-9]+\.azurecr\.io)/ai4ia/"
            rf"(?P<repository>{service}-[a-z0-9]+(?:-[a-z0-9]+)*)"
            r"@sha256:[0-9a-f]{64}"
        )
        old_image = containers[0]["image"]
        old_match = acr_image.fullmatch(old_image)
        new_match = acr_image.fullmatch(candidate_image)
        expected_repository = old_match["repository"] if old_match is not None else ""
        if service == "web":
            app_name = re.fullmatch(
                r"ca-web-(?P<environment>[a-z0-9]+(?:-[a-z0-9]+)*)",
                app["name"],
            )
            if app_name is None:
                raise ValueError("web resource name must identify its exact environment")
            expected_repository = "web-" + app_name["environment"]
        if (
            old_match is None
            or new_match is None
            or old_match["repository"] != expected_repository
            or old_match["registry"] != new_match["registry"]
            or old_match["repository"] != new_match["repository"]
            or old_image == candidate_image
        ):
            raise ValueError(
                f"candidate must be a new digest-pinned image in R0's exact {service} repository"
            )
        template["containers"][0]["image"] = candidate_image
        payload["properties"]["template"] = template
        return payload
    raise ValueError("unsupported stage")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--revision", type=Path, required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--stage", choices=("C1", "C2", "C3"), required=True)
    parser.add_argument("--service", choices=("api", "web"), default="api")
    parser.add_argument("--suffix", default="")
    parser.add_argument("--api-image", default="")
    parser.add_argument("--web-image", default="")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--classification-sidecar", type=Path)
    parser.add_argument(
        "--existing-revisions", type=Path,
        help="JSON rows from private_revision_rows; this offline helper cannot prove pagination completeness",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.stage == "C3" and (
            args.manifest is None or args.classification_sidecar is None
        ):
            raise ValueError(C3_BLOCKER)
        if args.stage != "C3" and (
            args.manifest is not None or args.classification_sidecar is not None
        ):
            raise ValueError("manifest inputs are accepted only for C3")
        private_paths = [
            path for path in (
                args.app, args.revision, args.existing_revisions,
                args.manifest, args.classification_sidecar, args.output,
            )
            if path is not None
        ]
        if any(path.resolve().is_relative_to(ROOT.resolve()) for path in private_paths):
            raise ValueError(
                "snapshot, manifest, and payload files must stay outside the repository"
            )
        payload = build_payload(
            json.loads(args.app.read_text(encoding="utf-8-sig")),
            json.loads(args.revision.read_text(encoding="utf-8-sig")),
            resource_id=args.resource_id,
            stage=args.stage,
            service=args.service,
            suffix=args.suffix,
            api_image=args.api_image,
            web_image=args.web_image,
            existing_revisions=(
                json.loads(args.existing_revisions.read_text(encoding="utf-8-sig"))
                if args.existing_revisions else None
            ),
            manifest_bytes=args.manifest.read_bytes() if args.manifest else None,
            classification_sidecar_bytes=(
                args.classification_sidecar.read_bytes()
                if args.classification_sidecar else None
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
