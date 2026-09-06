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
- Keep compatible HTTP/SSE model traffic behind SimpleL7Proxy -> APIM.
  Realtime/Voice Live WebSockets bypass SimpleL7Proxy only and remain behind the
  authenticated FastAPI relay -> APIM path.
- Preserve the one direct model exception: Responses-API Code Interpreter calls
  Foundry directly because its stateful Azure-managed sandbox is not a routable
  chat-completions deployment. That call site must keep pre-I/O entitlement
  enforcement, `store=false`, attempt metering, and managed-identity/resource-key
  isolation. Any other direct model call is a security architecture change.
- Preserve Entra/dev-auth boundaries, admin gates, per-user ownership checks, entitlement checks, and SSRF protections.
- Session/run tool auto-approval requires explicit owner consent and the
  default-off operator gate. Revalidate the consent and its enabled-tool scope
  at execution; new tools, changed contracts, expiry, or revocation cannot reuse
  an old grant. It must never bypass authorization, destination or usage limits,
  or suppress activity and execution receipts.
- Treat feature enablement as a security and cost change; wire server-side validation before surfacing UI.
