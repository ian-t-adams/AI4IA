# site — AI4IA self-documenting portal

A framework-free static site that documents AI4IA end to end and shows a
**timestamped status snapshot** of the deployed Azure environment. It links to the app
(<https://ai4ia.nomad-analytics.com>) but stands alone, and is published to GitHub Pages
at **<https://ian-t-adams.github.io/AI4IA/>**.

## Pages

| Page | What it shows |
| --- | --- |
| `index.html` | Purpose, capabilities, technology stack and live environment facts. |
| `status.html` | Twice-daily deployment snapshot: Azure resource health + public endpoint reachability. |
| `architecture.html` | Diagrams: request flow, deployment topology, identity/RBAC, MCP planes, chat-turn sequence. |
| `services.html` | Every Azure service AI4IA deploys, why it exists, its IaC module and RBAC. |
| `requirements.html` | IaC modules, the permissions/RBAC model, and package dependencies. |
| `docs.html` | Curated index into the repository's Markdown documentation. |

## How it is built

There is **no build step**. Pages are plain HTML; all logic is in one external script
(`assets/app.js`, so it also works under the app's strict nonce CSP if served from the
web container), and diagrams render with a vendored Mermaid (`assets/vendor/mermaid.min.js`,
so they work offline). Data is delivered as small JS files that assign `window.*` globals
(no `fetch`/CORS, so the site works from `file://` too).

```text
site/
  *.html            pages (relative links -> work under the /AI4IA/ Pages base path)
  assets/           styles.css, app.js, branding, vendored mermaid
  data/
    meta.js         app metadata, features, stack           (hand-maintained)
    services.js     Azure service catalogue                 (hand-maintained)
    requirements.js IaC modules, RBAC, packages             (hand-maintained)
    docs.js         documentation index                     (generated)
    inventory.js    timestamped resource inventory          (generated)
    status.js       timestamped health snapshot             (generated)
```

## Data

- `inventory.js` and `status.js` are **generated** by
  [`scripts/status-snapshot.ps1`](../scripts/status-snapshot.ps1), which reads the live
  environment through **Azure Resource Graph** (the authoritative inventory — `az resource
  list` omits several provider types AI4IA actually uses) plus Azure Resource Health, and
  probes the public endpoints. Run it with an `az login` that can read the subscription:

  ```powershell
  ./scripts/status-snapshot.ps1
  ```

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
Missing repository variables or failed Azure authentication stop publication rather than
shipping committed seed data as if it were current.
