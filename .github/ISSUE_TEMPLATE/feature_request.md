---
name: Feature request
about: Propose an AI4IA capability or improvement
title: "[Feature]: "
labels: [enhancement]
assignees: []
---

## Problem or opportunity

<!-- What user/operator need does this address? -->

## Proposed solution

<!-- Describe the desired behavior. -->

## Affected area

- [ ] Web (`app/web`)
- [ ] API (`app/api`)
- [ ] Agents / tools
- [ ] Model catalog / routing
- [ ] Infra / deployment
- [ ] Docs / site
- [ ] Other

## Governance considerations

- [ ] Uses APIM + SimpleL7Proxy for model traffic.
- [ ] Avoids hardcoded model/deployment names; uses `infra/models.json` where relevant.
- [ ] Has server-side feature gates and fail-closed prerequisites.
- [ ] Preserves per-user data isolation and Cosmos canonical data.
- [ ] Re-checks tool scopes, approvals, egress hosts, and SSRF protections if tools are involved.

## Alternatives considered

<!-- What else did you consider? -->

## Additional context

<!-- Links, screenshots, customer/demo requirements, or rollout notes. -->
