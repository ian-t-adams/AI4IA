# Azure Deployment Plan

> **Status:** Validated (deployment explicitly out of scope)

Generated: 2026-07-15T15:00:57-05:00

---

## 1. Project Overview

**Goal:** Remove the final production APIM deployment blocker by ensuring every
generated and static APIM policy payload stays below a conservative 14 KiB raw
UTF-8 ceiling, while preserving the existing governed gateway behavior.

**Path:** Add Components / Modify Existing

**Baseline:** Clean branch from `origin/main` at
`a67dc3149a62b81d4cf1279f5e975cdf71e92322` (PR #170).

**Approved scope:** Autonomous recovery was explicitly approved. Prepare,
validate, commit, push, and open a pull request. Do not deploy.

---

## 2. Production Failure and Readiness Invalidation

The previous deployment-ready assessment is invalid.

- Failed production deployment:
  [run 29446156016](https://github.com/ian-t-adams/AI4IA/actions/runs/29446156016)
- Commit: `a67dc3149a62b81d4cf1279f5e975cdf71e92322`
- Result: Sora validation and APIM expression compilation passed, but ARM
  provisioning failed with:
  `ValidationError: Policy size exceeds allowed limit of 16 KB`.
- Offending generated catalog fragments were 33,987 and 31,583 raw UTF-8 bytes.
- The setup fragment was 9,346 bytes and within the production limit.
- Live fragment PUT/full-chain compilation did not expose the production
  template's 16 KB validation path. A local hard ceiling is therefore mandatory.

No deployment may be attempted from this branch. Readiness can only be restored
after all validation in this plan passes and a separate deployment approval is
given.

---

## 3. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Production |
| Scale | Existing multi-region AI4IA deployment |
| Budget | No resource/SKU changes |
| Subscription | `sub-planetexpress-slurmfactory` (`ca68cf94-f445-43f1-8379-3d0100e293a2`) for validation and what-if only |
| Location | East US 2 (`eastus2`) |
| Deployment | Prohibited for this task |

---

## 4. Components Detected

| Component | Type | Technology | Path |
|-----------|------|------------|------|
| Gateway policy generator | Build-time generator/validator | Python 3.12 | `scripts/gen-gateway-policy.py` |
| APIM policy tests | Unit/integration validation | Python unittest | `scripts/tests/test_gateway_policy.py` |
| Live APIM compiler harness | Ephemeral validation | PowerShell/Azure REST | `scripts/test-apim-policy-compiler.ps1` |
| APIM infrastructure | Infrastructure as code | Bicep/AZD | `infra/modules/gateway.bicep` |
| Gateway policy artifacts | APIM raw XML | XML | `infra/policies/` |
| Application | Web/API/proxy | Next.js, FastAPI, SimpleL7Proxy | `app/`, `proxy/` |

---

## 5. Recipe Selection

**Selected:** Existing AZD + Bicep

**Rationale:** AI4IA already uses AZD and Bicep. This recovery is a surgical
change to the authoritative generator, generated raw XML, APIM resources, and
validation harness. No new Azure service, SKU, identity, or secret is required.

---

## 6. Architecture and Invariants

Compatible HTTP/SSE model traffic remains:

`Browser/API -> SimpleL7Proxy -> APIM -> Foundry`

Realtime/Voice Live WebSockets remain:

`Browser -> FastAPI relay -> APIM -> Foundry`

The fix must preserve:

- catalog-driven model routing from `infra/models.json`
- compiler-safe URL construction from PR #170
- retry, requeue, authentication, and loop-prevention behavior
- Sora API version and realtime policy split
- immutable policy fragment rollout and API-policy switch ordering
- no direct Foundry deployment calls from application code

---

## 7. Implementation Plan

1. Replace the obsolete 48 KiB fragment assumption with a 14 KiB raw UTF-8
   ceiling applied to complete policy payloads.
2. Deterministically split the catalog across a fixed safe slot count, emit
   empty placeholders when needed, and move to immutable `_33` fragment names.
3. Include all catalog fragments in order before setup, merge their variables
   without entry loss or duplication, and make API policy updates depend on all
   new fragment resources.
4. Validate generated catalog fragments, setup, API, realtime, and other APIM
   policy payloads relevant to the limit.
5. Add unit coverage for payload byte ceilings, an input above 16 KiB, stable
   ordering/placeholders, catalog identity preservation, variable merging, and
   compiled ARM dependencies.
6. Harden the live compiler harness to reject oversized local files before
   creating Azure resources, compile the full chain with unique temporary
   names, clean up exact names only, and verify absence without touching
   production fragments.
7. Run all required local, ARM, what-if, live compiler, specialist, and final
   review checks.

---

## 8. Execution Checklist

### Phase 1: Planning

- [x] Analyze workspace in MODIFY mode
- [x] Gather production failure requirements
- [x] Confirm inherited approved scope and no-deploy boundary
- [x] Scan gateway generator, tests, Bicep, policy artifacts, and harness
- [x] Select existing AZD + Bicep recipe
- [x] Preserve approved gateway architecture and governance invariants
- [x] User approved autonomous recovery in the task handoff

### Phase 2: Execution

- [x] Load Azure preparation and Azure AI code-generation guidance
- [x] Implement conservative payload sizing and deterministic catalog splitting
- [x] Generate immutable `_33` policy fragments
- [x] Update Bicep resource order and dependencies
- [x] Harden tests and live compiler harness
- [x] Regenerate checked-in policy artifacts
- [x] Set status to `Ready for Validation`

### Phase 3: Validation

- [x] Invoke `azure-validate`
- [x] Generator drift checks
- [x] Six JSON schema checks
- [x] Catalog and feature-prerequisite checks
- [x] Full gateway unit tests
- [x] Ruff
- [x] Bicep build
- [x] Compiled ARM payload sizes, fragment order, and dependencies
- [x] Full API tests because shared scripts are affected
- [x] Production gateway what-if with zero deletes
- [x] Live full-chain APIM compilation and exact cleanup verification
- [x] Bicep specialist review
- [x] Final code review
- [x] Record validation proof and set status to `Validated`

### Phase 4: Delivery

- [ ] Commit with Copilot coauthor
- [ ] Push branch
- [ ] Open pull request using repository template
- [ ] Report PR and exact maximum policy payload sizes/checks
- [ ] Do not deploy

---

## 9. Validation Proof

| Check | Command Run | Result | Timestamp |
|-------|-------------|--------|-----------|
| Generator drift | `gen-model-catalog.py --check`; `gen-mcp-catalog.py --check`; `gen-gateway-policy.py --check`; `gen-docs-catalog.py --check` | Pass | 2026-07-15T16:33:57-05:00 |
| Six schemas | `check-jsonschema` for models, MCP, toolbox current/example, routine, and A2A | Pass | 2026-07-15T16:25:00-05:00 |
| Catalog/prerequisites | `validate-catalog.py`; `validate-feature-prereqs.py` | Pass: 38 models, 70 deployments, 3 regions | 2026-07-15T16:25:00-05:00 |
| Gateway tests | `python -m unittest scripts.tests.test_gateway_policy` | Pass: 30 tests | 2026-07-15T16:30:00-05:00 |
| Python quality | `ruff check .`; `pyright` in `app/api` | Pass: 0 errors | 2026-07-15T16:25:00-05:00 |
| PowerShell quality | `Invoke-ScriptAnalyzer scripts/test-apim-policy-compiler.ps1` | Pass | 2026-07-15T16:25:00-05:00 |
| API tests | `pytest -q` in `app/api` | Pass: 1,546 tests | 2026-07-15T16:25:00-05:00 |
| Bicep/ARM | `az bicep build --file infra/main.bicep --stdout`; resolved compiled gateway ARM assertions | Pass: 21 fragments; max 14,071 bytes; shell 1,690; realtime 2,453; order/dependencies correct | 2026-07-15T16:33:00-05:00 |
| Production what-if | `az deployment group what-if` for `gateway` in `rg-ai4ia-slurmfactory` | Pass: 0 deletes, 21 immutable creates | 2026-07-15T16:20:00-05:00 |
| Live APIM compiler | `test-apim-policy-compiler.ps1` against `apim-ai4ia-slurmfactory` | Pass: all fragments, full model chain, realtime, exact API/fragment cleanup | 2026-07-15T16:23:00-05:00 |
| Specialist/final review | Bicep specialist and code-review agents | Pass | 2026-07-15T16:32:00-05:00 |

**Validated by:** `azure-validate` skill
**Validation timestamp:** 2026-07-15T16:33:57-05:00

---

## 10. Files Expected to Change

| File | Purpose | Status |
|------|---------|--------|
| `.azure/plan.md` | Approved recovery plan and validation record | Complete |
| `scripts/gen-gateway-policy.py` | Deterministic safe splitting and hard limits | Complete |
| `scripts/tests/test_gateway_policy.py` | Limit, integrity, and ARM ordering coverage | Complete |
| `scripts/test-apim-policy-compiler.ps1` | Preflight sizing and exact ephemeral cleanup | Complete |
| `infra/modules/gateway.bicep` | Immutable fragments and switch dependencies | Complete |
| `infra/policies/*.xml` | Regenerated safely sized policy payloads | Complete |

---

## 11. Research Summary

- Azure preparation guidance requires plan-first modification of the existing
  AZD+Bicep topology and validation before any deployment.
- Azure AI best practices reinforce retaining managed gateway routing,
  catalog-driven configuration, least-privilege identity, and no secret changes.
- The observed production ARM validation error is stricter than the earlier live
  compiler path, so repository validation must enforce the conservative raw
  UTF-8 payload ceiling independently of live compilation.

---

## 12. Current Step

Commit, push, and open the pull request. Do not deploy.
