## Summary

<!-- What changed and why? -->

## Validation

<!-- Paste relevant command output or explain why a check is not applicable. -->

- [ ] Web checks, if `app/web` changed: `npm ci`, `npm run lint --if-present`, `npm test`, `npm run build --if-present`
- [ ] API checks, if `app/api` changed: `ruff check .`, `pyright`, `pytest -q`
- [ ] Catalog checks, if model/MCP catalogs changed: `python scripts/gen-model-catalog.py --check`, `python scripts/gen-mcp-catalog.py --check`, `python scripts/validate-catalog.py`
- [ ] Infra checks, if `infra`/`foundry` changed: schema validation, `python scripts/validate-feature-prereqs.py`, `bicep build infra/main.bicep --stdout`
- [ ] Operational quality, if workflows/scripts/Docker/YAML changed: actionlint/PSScriptAnalyzer/hadolint/yamllint as applicable

## Governance checklist

- [ ] Model traffic still goes through APIM + SimpleL7Proxy; no direct Foundry model calls were added.
- [ ] No hardcoded deployment names or model lists were added; `infra/models.json` remains the source of truth.
- [ ] Feature gates are enforced server-side, not only in the web app.
- [ ] Per-user ownership, Cosmos partitioning, and derived-store rebuild assumptions are preserved.
- [ ] Tool changes re-check scopes, approvals, egress hosts, and SSRF protections at execution time.
- [ ] Docs/runbooks were updated for user-visible, config, deployment, or operational changes.

## Risk and rollout

<!-- Include migration, cost, security, and rollback notes. -->
