# Responsible AI decision record: annotate-only content filtering

> **STATUS: approval evidenced; review cadence still unset.**
>
> Assembled from the implemented state on 2026-08-05, updated 2026-08-06 once the
> approval evidence was verified against the live control plane. The one field
> that remains genuinely open is the **expiry / review cadence** — that is a
> judgement, not a fact, and no amount of inspection produces it.

## Decision

Every Azure AI Content Safety filter on every model deployment is **enabled but
non-blocking**. Foundry evaluates each request and response, returns a verdict, and
the platform records and displays it. Nothing is refused, rewritten, or withheld on
the basis of that verdict.

## Approval

| Field | Value |
| --- | --- |
| Accountable owner | Ian Adams (repository owner; the address on the production alert action group) |
| Approval evidence | The deployed policy itself — see below |
| Approval reference | Held by the owner (Azure guardrails-modification approval email); not stored in the repo |
| Scope | All Foundry model deployments in every region, all users |
| Expiry / next review | **Unset** — see "What would change this decision" |

**Why the deployed policy is the approval artifact.** Azure's control plane refuses
a RAI policy that disables blocking on the abuse filters unless the subscription
holds an approved modification request. Verified against the live account
`mf-aiforia-slurmfactory-eastus2-vypvgrncoed2o` on 2026-08-06:

| Policy | Filters | Non-blocking |
| --- | --- | --- |
| `ai4ia-annotate-only` (base `Microsoft.DefaultV2`) | 11 | **11** |
| `Microsoft.DefaultV2` | 11 | 1 |
| `Microsoft.Default` | 8 | 0 |

A policy turning off blocking on `jailbreak`, `protected_material_text` and
`protected_material_code` exists, was accepted, and is applied. That state is not
reachable without the exception, so the claim in
`scripts/tests/test_rai_policy.py` is evidenced rather than unsupported. What the
deployed policy does **not** establish is who is accountable, when the exception
should be revisited, or what would invalidate it — which is the actual purpose of
this record.

## What is actually configured

From `infra/modules/foundry.bicep`, applied to every deployment:

| Filter | Source | Enabled | Blocking | Severity threshold |
| --- | --- | --- | --- | --- |
| hate | Prompt, Completion | yes | **no** | High |
| sexual | Prompt, Completion | yes | **no** | High |
| selfharm | Prompt, Completion | yes | **no** | High |
| violence | Prompt, Completion | yes | **no** | High |
| jailbreak (Prompt Shield) | Prompt | yes | **no** | n/a |
| protected_material_text | Completion | yes | **no** | n/a |
| protected_material_code | Completion | yes | **no** | n/a |

Two details worth stating plainly, because both are easy to misread:

- **`severityThreshold: 'High'` does not mean "block high severity".** With
  `blocking: false` it only sets the level at which an annotation is raised. Nothing
  is blocked at any severity.
- **The jailbreak and protected-material filters ship blocking by default** in
  `Microsoft.DefaultV2`. They are non-blocking here only because they are explicitly
  overridden. Omitting them from the override list would silently leave them
  blocking — which is why `test_rai_policy.py` checks all seven rather than the four
  harm categories.

## Compensating controls

**Implemented.** Annotations are no longer discarded. Foundry returns a verdict for
every category on every turn; before 2026-08-04 the platform threw all of it away, so
the safety system ran on every request and was completely invisible. Verdicts are now
normalized (`app/api/src/ai4ia_api/safety.py`), persisted on the message, and shown in
a per-turn panel that states plainly that nothing was blocked or rewritten.
`filtered: true` is always treated as notable, so a future switch to blocking would
surface rather than change behaviour silently.

**Not implemented, and needed before this can be called a controlled exception:**

1. **No aggregate signal.** Annotations are visible per message. Nothing counts them,
   alerts on a spike, or lets anyone answer "how often did the jailbreak filter fire
   last week?"
2. **No escalation path.** A high-severity annotation on a completion produces a UI
   note and nothing else. There is no queue, no reviewer, no notification.
3. **No user-facing disclosure.** Users are not told that outputs are unfiltered.
4. **No tested moderation boundary.** The audit's premise for accepting an
   annotate-only posture was "a documented and validated replacement boundary". The
   replacement is currently visibility only.

## Why this is not simply wrong

Stated fairly, since the decision may well be correct for this platform: this is a
single-tenant internal tool with named, authenticated users, and the products it is
compared against block content that legitimate technical work needs — security
research, incident write-ups, and code that trips protected-material heuristics.
Blocking filters in that setting produce refusals that look like malfunctions and
push users toward ungoverned tools, which is a worse outcome than an annotated
response.

That argument depends on the user population staying small, known and internal. It
stops holding the moment the platform is exposed to a second tenant or to
unauthenticated use — which is also when audit finding P1-10 (tenant-public means
application-public) stops being latent. Tie the expiry above to that, not only to a
date.

## What would change this decision

- Onboarding a second tenant, or any unauthenticated access.
- A regulatory or customer commitment that requires enforced filtering.
- Evidence from the aggregate signal (once it exists) that a category fires often
  enough to constitute a real pattern rather than noise.

## References

- Implementation: `infra/modules/foundry.bicep`
- Invariants: `scripts/tests/test_rai_policy.py`
- Annotation surfacing: `app/api/src/ai4ia_api/safety.py`,
  `app/web/src/components/MessageList.tsx`
- Audit finding: [P0-2 in the repository audit](./repository-audit-2026-08-03.md)
