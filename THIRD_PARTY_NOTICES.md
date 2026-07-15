# Third Party Notices

AI4IA depends on third-party open-source software through its application packages, container base images, GitHub Actions, and a vendored model gateway. This file highlights the notable sources; package managers and lock files remain the dependency-level source of truth.

## Vendored SimpleL7Proxy

The `proxy/` tree vendors source from [`microsoft/SimpleL7Proxy`](https://github.com/microsoft/SimpleL7Proxy), pinned in `proxy/README.md` and `proxy/Dockerfile` to commit `d9eb1d1fa42820792a9699bfc253562fba07d977`.

Vendored directories:

- `proxy/Shared/`
- `proxy/Shared-parser/`
- `proxy/SimpleL7Proxy/`

Upstream license: MIT License, copyright Microsoft Corporation. A copy of the upstream MIT license is kept at `proxy/LICENSE`. Keep the pinned commit, this notice, `proxy/README.md`, and its documented AI4IA deviations in sync whenever the vendored copy is refreshed.

## Application dependencies

- `app/web` uses Next.js, React, TypeScript, ESLint, Vitest, Testing Library, MSAL browser packages, and their transitive npm dependencies. See `app/web/package.json` and `app/web/package-lock.json`.
- `app/api` uses FastAPI, Pydantic, httpx, Azure SDKs, PyJWT, pytest, ruff, pyright, mem0, Web IQ, OpenTelemetry/Azure Monitor packages, and their transitive Python dependencies. See `app/api/pyproject.toml` and `app/api/uv.lock`.
- `proxy` builds .NET 10 SimpleL7Proxy and NuGet dependencies from the vendored project files.

## Tooling, containers, and actions

- Container images are based on Microsoft .NET images, Python slim images, and Node Alpine images as declared in Dockerfiles.
- GitHub Actions workflows use pinned actions and tools such as CodeQL, Trivy, gitleaks, actionlint, PSScriptAnalyzer, hadolint, yamllint, Bicep, and check-jsonschema.

This notice is not a generated software bill of materials. For release or redistribution, generate dependency-specific license reports from the lock files and image manifests.
