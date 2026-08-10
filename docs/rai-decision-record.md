# Responsible AI decision record: annotate-only content filtering

> **DOCUMENT STATUS: complete. CONTROL APPROVAL STATUS: incomplete.**
> The live control plane proves that Azure accepted the annotate-only policy for
> the deployed model resources. It does **not** prove that the accountable owner
> re-approved that risk for image, video, Azure OpenAI Voice Live, or Speech Voice
> Live. All four modalities are enabled in the current environment, so review
> trigger 3 has fired. No modality-scope approval artifact is present in this
> repository; do not infer one.
>
> Assembled from the implemented state on 2026-08-05. The review cadence is a
> judgement rather than a fact — the default recorded here was proposed by the
> implementer and stands until the owner replaces it. Change the row and the
> triggers together; a date without triggers is the weaker half.

## Decision

The `ai4ia-annotate-only` policy configured on the catalog model deployments is
**enabled but non-blocking**. For text chat completions, Foundry returns safety
annotations and AI4IA normalizes, persists, and displays them without refusing,
rewriting, or withholding the text. This repository does not evidence equivalent
annotation capture/persistence/display for image, video, Azure OpenAI Voice Live,
or Speech Voice Live.

## Approval

| Field | Value |
| --- | --- |
| Accountable owner | Ian Adams (repository owner; the address on the production alert action group) |
| Azure exception evidence | The deployed text-policy configuration itself — see below |
| Azure approval reference | Reported as held by the owner (guardrails-modification approval email); not stored in the repo |
| Decision scope actually reasoned about | Text completions for named, authenticated internal users |
| Enabled but not re-approved in this record | Image, video, Azure OpenAI Voice Live, Speech Voice Live |
| Control status | **Incomplete pending modality-scope re-approval and compensating-control evidence** |
| Next scheduled review | Annual review was proposed for **2027-08-06**, but trigger 3 requires review now rather than waiting |
| Invalidated immediately by | Any trigger in "Review triggers" below — these do not wait for the annual date |

**What the deployed policy proves.** Azure's control plane refuses
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
`scripts/tests/test_rai_policy.py` is evidenced rather than unsupported. What the deployed policy does **not** establish is approval of AI4IA's modality
scope, who accepted each modality's application risk, or whether modality-specific
monitoring and escalation are adequate. Azure accepting a resource policy and an
owner approving how this application uses every enabled modality are separate
facts.

## Evidence required to close the control

The document is structurally complete; the control is not. Closing it requires
all of the following without inventing or backdating approval:

1. A dated accountable-owner decision that explicitly names text, image, video,
   Azure OpenAI Voice Live, and Speech Voice Live.
2. The Azure guardrails-modification approval reference and its applicable
   resource/modality scope, retained in the approved evidence system.
3. Modality-specific abuse cases, user disclosure, monitoring, escalation owner,
   and response procedure.
4. Evidence that annotations or equivalent safety signals are collected for each
   enabled modality, plus a tested alert/escalation path for actionable events.
5. A recorded accept/mitigate/disable decision for each modality and a new review
   date after the trigger-driven review completes.

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

**Implemented for text chat completions.** Those annotations are no longer
discarded: verdicts returned on the text-completion path are normalized
(`app/api/src/ai4ia_api/safety.py`), persisted on the message, and shown in a
per-turn panel that states plainly that the displayed text was not blocked or
rewritten. Before 2026-08-04 that chat-path evidence was thrown away.
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
5. **No modality evidence.** Image, video, and both Voice Live providers are
   enabled, but this record has no evidence that their safety outputs are
   normalized, persisted, displayed, aggregated, or escalated.

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
application-public) stops being latent. That is trigger 1 in "Review triggers"
below, and it is why the review is trigger-driven rather than only annual.

## Review triggers

The annual date is the *backstop*, not the control. The argument above depends on
facts that can change without anyone revisiting this page, so each of these
invalidates the decision the day it happens and requires re-approval before the
annotate-only posture continues:

| # | Trigger | Why it breaks the argument | How you would notice |
| --- | --- | --- | --- |
| 1 | A second Entra tenant is allowed, or any unauthenticated access is enabled | The whole justification is "small, known, internal, authenticated". This is also the moment P1-10 (tenant-public means application-public) stops being latent. | Startup already **refuses** when more than one tenant is allowed, so this cannot happen silently — the refusal is the notification. |
| 2 | A high-severity annotation is observed on a production completion | The premise is that filters would fire on legitimate technical work, not on genuinely harmful content. One high-severity hit is evidence the premise is wrong. | `AppEvents` in the Log Analytics workspace. **Nothing alerts on this today** — see the gap below. |
| 3 | A new output modality is enabled that this record did not reason about | The decision was made about text completions. Image, video, and realtime voice have different failure modes and blast radii. **Fired:** image, video, Azure OpenAI Voice Live, and Speech Voice Live are enabled; modality re-approval is not evidenced here. | Repository/deployment feature posture plus the evidence list above. |
| 4 | The Azure guardrails-modification approval lapses, or a deployment is recreated on a stock policy | The approval *is* the deployed policy. If the policy reverts, the exception has already ended in fact. | `scripts/tests/test_rai_policy.py` pins the posture in IaC; a live drift would need a control-plane read. |
| 5 | A regulatory or customer commitment requires enforced filtering | External obligation overrides the internal tradeoff. | Owner judgement. |

> **Known gap in trigger 2.** There is no alert on high-severity annotations —
> they land in telemetry and nothing reads them. That makes the most
> evidence-driven trigger the one least likely to fire on time. Until an alert
> exists, treat trigger 2 as "checked at the annual review", not "detected".
> This is the same gap the "No escalation path" limitation records above; it is
> repeated here because it directly weakens a control this record depends on.

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
