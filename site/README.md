# site — AI4IA self-documenting portal

A framework-free static site that documents AI4IA end to end and shows the **live
status/health** of the deployed Azure environment. It links to the app
(<https://ai4ia.nomad-analytics.com>) but stands alone, and is published to GitHub Pages
at **<https://ian-t-adams.github.io/AI4IA/>**.

## Pages

| Page | What it shows |
| --- | --- |
| `index.html` | Purpose, capabilities, technology stack and live environment facts. |
| `status.html` | Live health of every deployed Azure resource + the public endpoints. |
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
    docs.js         documentation index                     (hand-maintained)
    inventory.js    live resource inventory                 (generated)
    status.js       live health snapshot                    (generated)
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

- The other `data/*.js` files are hand-maintained documentation content; refresh them when
  infra, RBAC, or dependencies change.

## Deploy

[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) publishes `site/` to GitHub
Pages on every push to `main` (and twice daily). On `main` it also refreshes the status
snapshot best-effort using the deploy identity's `ref:refs/heads/main` federated
credential; if that is unavailable it simply ships the committed seed data.
