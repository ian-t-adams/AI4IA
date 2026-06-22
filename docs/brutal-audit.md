# Brutal Repo Audit

This repo is ambitious, useful, and dangerously tolerant of operator pain. The
architecture is not the embarrassing part; the embarrassing part is how many
ways the setup can look green while the deployment is stale, mis-owned,
half-configured, or expensive.

## Verdict

AI4IA is a serious Azure AI workload wearing a demo repo hoodie. It has real
governance ideas: APIM/SimpleL7Proxy in front of model calls, managed identity,
feature gates, Cosmos as canonical state, rebuildable derived stores, and
Container Apps for deployable services. Good.

Now the yelling: the repo used to ship personal IaC defaults, tolerate
`npm install` in production image builds, document custom-domain outages as if
that made them less outage-shaped, and rely on prose instead of CI for obvious
configuration contradictions. Documentation existed, but it was scattered
enough that an operator had to play archaeology with `main.bicep`,
`main.parameters.json`, workflow variables, app env, and runbooks.

## What was fixed in this audit

| Area | Before | Fixed |
| --- | --- | --- |
| Web builds | Docker and CI could fall back from `npm ci` to `npm install`, destroying reproducibility. | Docker and CI now use deterministic `npm ci`; Docker uses Node 22 to match CI. |
| Web runtime | No route-level error boundary, so a client render crash could dump users into default framework failure UI. | Added `app/web/src/app/error.tsx` with a recoverable error screen. |
| API image | Non-root runtime existed, but the image had no container-native health check. | Added a Docker `HEALTHCHECK` against `/health/live`. |
| IaC ownership | Personal owner/email defaults leaked into tags, budgets, and APIM publisher config. | Replaced personal defaults with neutral deployment-owned defaults and wired `AI4IA_APIM_PUBLISHER_EMAIL`. |
| Infra validation | CI validated Bicep syntax and model catalog shape, not contradictory deployment settings. | Added `scripts/validate-feature-prereqs.py` to `infra-validate.yml`. |
| Config docs | Feature/env/parameter mapping was scattered. | Added `docs/configuration-reference.md`. |

## The ugly parts that remain

| Problem | Why it matters | Where to look |
| --- | --- | --- |
| Component tests are basically absent. | A chat app with voice, documents, agents, admin, and streaming should not rely on pure helper tests and crossed fingers. | `app/web/vitest.config.ts`, `app/web/src/components/` |
| API error responses are inconsistent. | Some routers use raw ints, some `status.HTTP_*`, some positional `HTTPException`; clients get a soup of `{detail: ...}` strings. | `app/api/src/ai4ia_api/routers/` |
| Type checking is missing from API CI. | Python typing exists in the code, but CI does not enforce it. That is cosplay, not type safety. | `app/api/pyproject.toml`, `.github/workflows/app-ci.yml` |
| Custom domains are still externally fragile. | Empty CI variables can remove vanity bindings during provision. The docs warn about it, but CI cannot see repo variables until deploy time. | `.github/workflows/deploy.yml`, `docs/runbooks/deployment.md` |
| Feature posture is expensive by design. | The checked-in live parameters turn on several costly advanced surfaces. That may be intentional for this environment, but it is not a starter posture. | `infra/main.parameters.json` |
| Post-provision is still mostly a reminder. | A deployment can finish without proving API, gateway, model deployments, DNS, or auth actually work. | `scripts/postprovision.ps1`, `azure.yaml` |

## First-party guidance this critique is grounded in

- [Azure Well-Architected Framework](https://learn.microsoft.com/azure/well-architected/what-is-well-architected-framework): workloads should balance reliability, security, cost, operational excellence, and performance. This repo is strongest on architecture intent and weakest on operational proof.
- [Azure Developer CLI GitHub Actions pipeline](https://learn.microsoft.com/azure/developer/azure-developer-cli/pipeline-github-actions): `azd` pipelines are meant to provision and deploy automatically. A green pipeline that cannot prove the app works is not enough.
- [API Management AI gateway capabilities](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities): APIM can govern model access, token limits, resiliency, and observability. AI4IA has the gateway pattern; it still needs stronger documented policy posture around token quotas and runtime dashboards.
- [Azure Container Apps architecture best practices](https://learn.microsoft.com/azure/well-architected/service-guides/azure-container-apps): Container Apps workloads need explicit reliability, health, revision, ingress, security, and observability decisions. The repo had probes in app routes, but the API image itself did not advertise a health check.

## Next fixes that should not be deferred

1. Add API type checking in CI with a realistic baseline, not a wall of ignored errors.
2. Normalize API error response shape and stop throwing raw integer status codes.
3. Add at least one DOM/component test harness for the web chat path.
4. Replace the post-provision reminder script with smoke tests that fail loudly.
5. Add custom-domain deploy validation that refuses to proceed when a live vanity hostname is expected but missing from CI variables.
