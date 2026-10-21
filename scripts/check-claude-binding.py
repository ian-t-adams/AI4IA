#!/usr/bin/env python3
"""Read-only, opt-in cross-tenant Claude binding gate. No login, writes or model calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _capacity_evidence import EvidenceError
from _claude_binding import configured_binding, verify_configured
from _claude_gateway import configured_gateway, source_apim_id, verify_gateway_routes, verify_gateway_transition


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate configuration offline, without constructing a reader.")
    parser.add_argument("--source-apim-resource-id", default="", help="Exact existing source APIM ID for upstream-gateway route readback.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--routed", action="store_true", help="Also verify attached identity and actual APIM policies after provision.")
    mode.add_argument("--target-preflight", action="store_true", help="Before separate target provision: empty dedicated group, exact GA offering/counter/raw capacity and retirement evidence.")
    args = parser.parse_args()
    try:
        binding = configured_binding()
        gateway = configured_gateway()
        if gateway is not None:
            if args.target_preflight:
                raise EvidenceError("claude_gateway_does_not_provision_models")
            if args.routed:
                verify_gateway_routes(args.source_apim_resource_id or source_apim_id())
                print("Claude upstream gateway: source policy routes and protected credential metadata verified; not live inference proof.")
            else:
                if not args.check:
                    verify_gateway_transition(args.source_apim_resource_id or source_apim_id())
                print("Claude upstream gateway: credential configuration valid; no federation or live inference proof.")
            return 0
        if args.check:
            print("Claude binding configuration: configured (not verified)." if binding else "Claude binding: disabled.")
            return 0
        models = json.loads((Path(__file__).resolve().parents[1] / "infra" / "models.json").read_text(encoding="utf-8"))
        count = verify_configured(models, routed=args.routed, target_plan=args.target_preflight)
    except (EvidenceError, ValueError, KeyError, TypeError) as exc:
        code = exc.code if isinstance(exc, EvidenceError) else "claude_readback_invalid"
        print(f"ERROR: {code}; Claude activation is unavailable. No mutation was attempted.")
        return 1
    label = "Claude target plan: available catalog intents (not a reservation)" if args.target_preflight else "Claude exact-target readback: deployments verified"
    print(f"{label}: {count}." if count else "Claude binding: disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
