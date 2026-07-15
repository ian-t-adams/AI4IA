# Security Policy

## Supported versions

This repository currently supports the `main` branch. Security fixes should target `main` unless a maintainer identifies a release branch that also needs a patch.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for suspected vulnerabilities. Use GitHub private vulnerability reporting for this repository so maintainers can triage and coordinate a fix before public disclosure.

Include, when possible:

- affected component and commit or version;
- clear reproduction steps or proof of concept;
- expected impact and any known prerequisites;
- whether credentials, tenant data, model traffic, or user data could be exposed;
- suggested mitigation, if known.

Maintainers should acknowledge the report in GitHub, assess severity, prepare a fix privately, and publish an advisory or release notes when disclosure is appropriate.

## Security expectations for contributors

- Do not commit secrets or customer data.
- Keep HTTP/SSE model traffic behind SimpleL7Proxy -> APIM and realtime behind the authenticated FastAPI relay -> APIM.
- Preserve Entra/dev-auth boundaries, admin gates, per-user ownership checks, entitlement checks, and SSRF protections.
- Treat feature enablement as a security and cost change; wire server-side validation before surfacing UI.
