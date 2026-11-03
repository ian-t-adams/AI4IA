# site — AI4IA self-documenting portal

A framework-free static site that documents AI4IA end to end and shows a
**anonymous uptime observations and a timestamped Azure status snapshot**. It links to the app
(<https://ai4ia.nomad-analytics.com>) but stands alone, and is published to GitHub Pages
at **<https://ian-t-adams.github.io/AI4IA/>**.

## Pages

| Page | What it shows |
| --- | --- |
| `index.html` | Purpose, capabilities, technology stack and generic deployment design. |
| `status.html` | 15-minute anonymous uptime observations and a separate twice-daily Azure deployment snapshot. |
| `architecture.html` | Diagrams: request flow, deployment topology, identity/RBAC, MCP planes, chat-turn sequence. |
| `services.html` | Every Azure service AI4IA deploys, why it exists, its IaC module and RBAC. |
| `requirements.html` | IaC modules, the permissions/RBAC model, and package dependencies. |
| `docs.html` | Curated index into the repository's Markdown documentation. |

## How it is built

There is **no build step**. Pages are plain HTML; all logic is in one external script
(`assets/app.js`, so it also works under the app's strict nonce CSP if served from the
web container), and diagrams render with a vendored Mermaid (`assets/vendor/mermaid.min.js`,
so they work offline). Data is delivered as small JS files that assign `window.*` globals
(so the documentation and Azure snapshot work from `file://` too). The uptime
panel fetches a small same-origin JSON report; unavailable local-file/network
access is shown as unknown, not healthy.

```text
site/
  *.html            pages (relative links -> work under the /AI4IA/ Pages base path)
  assets/           styles.css, app.js, branding, vendored mermaid
  data/
    meta.js         app metadata, features, stack           (hand-maintained)
    services.js     Azure service catalogue                 (hand-maintained)
    requirements.js IaC modules, RBAC, packages             (hand-maintained)
    docs.js         documentation index                     (generated)
    inventory.js    timestamped generic service inventory   (generated; unobserved seed)
    status.js       timestamped health snapshot             (generated)
    uptime.json     anonymous uptime and incident counters  (generated; seed is unobserved)
```

## Data

- `inventory.js` and `status.js` are **generated** by
  [`scripts/status-snapshot.ps1`](../scripts/status-snapshot.ps1), which reads the live
  environment through **Azure Resource Graph** (the authoritative inventory —
  `az resource list` omits several provider types AI4IA actually uses) plus Azure
  Resource Health, and probes the public endpoints. The required inventory query
  must return a non-empty, well-shaped `data` array; CLI, JSON, or shape failures
  stop before either generated file is replaced. Resource Health provider or
  query failures are published explicitly as a source outage rather than as zero
  healthy resources. `Microsoft.ResourceHealth` is an operational dependency in
  `scripts/check-resource-providers.py`, so the deploy preflight registers it.
  Run the snapshot with an `az login` that can read the subscription:

  ```powershell
  ./scripts/status-snapshot.ps1 -Public
  ```

  Direct API checks use `AZURE_API_URL` from the selected azd environment or
  `-ApiUrl https://<api-host>`. Without either, the same scoped Resource Graph
  inventory must identify exactly one public Container App with
  `azd-service-name=api`; a missing, ambiguous or private target is reported as
  unobserved, never guessed from an app name. The URL must be a credential-free
  HTTPS DNS origin. Pages uses this inventory discovery and needs no additional
  identity, repository variable or Azure role.

  `scripts/status-endpoints.ps1` performs anonymous GETs of `/health/live` and
  `/health/ready`, with no cookies, default credentials or redirects. Each API
  request has a 20-second total deadline and a 4 KiB UTF-8 JSON body limit.
  Only the API's expected HTTP 200/status contract passes; readiness also requires
  `stage=session_store`. An expected readiness 503 is a persistence failure;
  auth challenges, redirects, malformed responses and transport failures have
  distinct non-passing outcomes. Response bodies, headers and exception details
  are never published. Each attempted probe records its time and elapsed
  milliseconds; unresolved targets retain null timing instead of inventing an
  observation. Endpoint failures produce a fresh snapshot of the failure, not a
  republished healthy seed. Historical snapshots without API observations show
  unknown coverage.

  The public projection requires timestamped, consistent state/status/outcome
  evidence before counting an endpoint as passing or down. Contradictions become
  unknown with a generic invalid-response explanation; summary counts and rendered
  cards use the same evidence rules. Ingress reachability includes 2xx/3xx and
  401/403, but API health challenges and redirects remain unknown.

  Liveness covers the process only. Readiness covers the API's cached canonical
  session-store check (success cache up to 15 seconds), not Search, user
  authentication or model/gateway traffic. These probes do not perform inference
  or mutate user data. The former scheduled model/GA setup monitor and its
  recovery CLI were removed; post-deployment smoke tests and explicit operator
  voice diagnostics remain separate.

- `uptime.json` is generated by [`scripts/uptime.py`](../scripts/uptime.py) through
  [`.github/workflows/uptime.yml`](../.github/workflows/uptime.yml), every 15 minutes.
  Web HTTP 200 HTML, the proxy's anonymous `/liveness` payload and the API's
  expected live/ready JSON are checked without authentication, model calls or
  user data writes. Three consecutive failures or unknown observations open an
  incident; only the new incident alerts, and a passing check recovers
  automatically. Missing history never blocks new probes. The report contains
  only generic service identifiers, timing, fixed outcomes, opaque target-continuity
  hashes and bounded counters, not endpoint URLs, response bodies,
  exception text, credentials or user identifiers.

  The page refreshes its report once per minute and marks evidence older than
  45 minutes unknown. GitHub scheduling can be delayed; these are sampled
  observations, not an uptime SLA. A successful workflow means collection and
  publication, not application health. See the
  [uptime runbook](../docs/runbooks/deployment.md#anonymous-uptime-monitoring)
  for the three endpoint variables and notification semantics.

- `docs.js` is **generated** by [`scripts/gen-docs-catalog.py`](../scripts/gen-docs-catalog.py)
  from [`data/docs.manifest.json`](data/docs.manifest.json) — the curated list of which repo
  Markdown files to surface and how to group them. Regenerate it (and verify no drift) with:

  ```powershell
  python scripts/gen-docs-catalog.py
  python scripts/gen-docs-catalog.py --check
  ```

  The `--check` mode is enforced by the `quality` CI workflow, and `pages.yml` regenerates it
  on every portal publish, so the documentation hub always tracks the current repo Markdown.

- The other `data/*.js` files (`meta.js`, `services.js`, `requirements.js`) are hand-maintained
  documentation content; refresh them when infra, RBAC, or dependencies change.

## Deploy

[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) publishes `site/` to GitHub
Pages on every push to `main` (and twice daily). Every publish refreshes the status snapshot
using the existing deploy identity's required `ref:refs/heads/main` federated credential.
Missing repository variables or failed Azure authentication stop that publication rather than
shipping committed seed data as if it were current.

The independent `uptime.yml` publisher performs no Azure login or inventory
queries. It shares the Pages publisher lock and preserves the last published
Azure snapshot's original timestamp (or shows unavailable if it cannot be
retrieved). The twice-daily publisher carries forward the uptime report, so
neither cadence silently overwrites the other's evidence.

## Public privacy boundary

Both entry workflows pass existing repository variables as explicit
`workflow_call` secrets to local publisher workflows. This masks configuration
before the runner displays step inputs/environment, including Azure login.
There is no new credential, role, variable migration, schedule or identity.
Azure CLI login output is disabled. Reader errors expose only fixed diagnostics,
not resource identifiers or provider response text.

[`scripts/public_status.py`](../scripts/public_status.py) reconstructs public
inventory and health from allowlisted types, labels, states, numbers and dates.
Names, IDs, regions, suffixes, backend URLs and arbitrary provider text never
cross this projection. The snapshot's `-Public` mode passes observations through
stdin in memory before writing site files. Private/operator output remains
available without `-Public`; an explicit output directory **outside `site/`**
is required (linked ancestors are refused before discovery), and do
not upload those diagnostics to public Actions artifacts or logs.

The uptime publisher reprojects even old detailed snapshots before carrying
them forward. Invalid or unavailable snapshots become explicitly unavailable,
never a checked-in healthy fallback. Version-1 uptime histories are migrated to
version 2 without publishing their URLs: a full SHA-256 target binding preserves
incident continuity and detects target changes. This is pseudonymization, not
proof that a guessed hostname cannot be recognized. Counters and original
observation timestamps survive; no new observation is invented.

Both publishers run `python -m scripts.public_status --check-site site` before
upload. It verifies canonical projected snapshot/report shapes and scans textual
site artifacts for Azure IDs/origins and known precise deployment-region labels.
Only fixed public UUID constants used by Mermaid/RFC namespaces are exempt;
vendor files are not broadly skipped. Generic architecture, service purposes,
setup/RBAC guidance and the normal public application link remain available.

This source change is not a live release or erasure of already public information.
Previously published Pages versions, workflow logs/artifacts, Git history,
third-party caches, and deployment-specific catalog/source/operator documentation
elsewhere in this public repository may still expose infrastructure details.
Removing or restricting that separate evidence needs a separately reviewed scope;
identifiers are not credentials, and obscuring them does not replace access controls.
