"""Construct C1/C2 JSON Merge PATCH bodies offline; refuse blocked C3 and never invoke Azure."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API_VERSION = "2025-01-01"
C3_BLOCKER = (
    "C3 is blocked until the exact production manifest is independently "
    "classified as nonconfidential; no production manifest was read"
)
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
) -> dict[str, Any]:
    """Require a quiescent R0 snapshot, then pin it in the same request."""
    if stage not in ("C1", "C2", "C3"):
        raise ValueError("stage must be C1, C2 or C3")
    if stage == "C3":
        raise ValueError(C3_BLOCKER)
    if service not in ("api", "web") or (service != "api" and stage != "C2"):
        raise ValueError("web service supports only C2 staging")
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
    parser.add_argument(
        "--existing-revisions", type=Path,
        help="JSON rows from private_revision_rows; this offline helper cannot prove pagination completeness",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.stage == "C3":
            raise ValueError(C3_BLOCKER)
        private_paths = [
            path for path in (args.app, args.revision, args.existing_revisions, args.output)
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
