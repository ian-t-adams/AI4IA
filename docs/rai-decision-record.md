# Responsible AI decision record: annotate-only content filtering

> **STATUS: DRAFT — NOT YET APPROVED.**
>
> This document was assembled from the implemented state on 2026-08-05. It records
> what the platform actually does, so that a decision can be signed rather than
> reconstructed. **The accountable owner, approval date, and expiry below are
> deliberately unfilled.** An agent cannot supply them; only a named human can.
>
> Until they are filled in, the honest description of this control is "an
> undocumented exception that a code comment asserts was approved."

## Decision

Every Azure AI Content Safety filter on every model deployment is **enabled but
non-blocking**. Foundry evaluates each request and response, returns a verdict, and
the platform records and displays it. Nothing is refused, rewritten, or withheld on
the basis of that verdict.

## Approval

| Field | Value |
| --- | --- |
| Accountable owner | *(unfilled — must be a named individual, not a team)* |
| Approved on | *(unfilled)* |
| Approval reference | *(unfilled — the Azure guardrails-modification exception id)* |
| Scope | All Foundry model deployments in every region, all users |
| Expiry / next review | *(unfilled — recommend 6 months maximum)* |
| Reviewed by | *(unfilled)* |

`scripts/tests/test_rai_policy.py` currently states that this posture "exists under
an approved Azure guardrails-modification exception". That claim is not evidenced
anywhere in the repository. Either the reference goes in the table above, or the
claim should come out of the test.

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
