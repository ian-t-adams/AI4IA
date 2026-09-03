# Responsible AI decision record: annotate-only content filtering

> **DOCUMENT STATUS: complete. OWNER POLICY DECISION: recorded 2026-09-03.
> CONTROL IMPLEMENTATION STATUS: incomplete.**
> The accountable owner directed that AI4IA must not add application-level
> blocking, rewriting, or withholding based on a guardrail assessment. Users
> should instead see every assessment the provider returns, including category,
> scope, detection state, and severity, and should see an explicit unavailable
> state when a provider or modality returns no assessment. Provider-native
> refusals and safety systems remain outside AI4IA's control and must not be
> described as if the application disabled them.
>
> This direction covers the currently served Azure OpenAI text, image, video,
> Azure OpenAI Voice Live, Speech Voice Live, and Black Forest Labs image paths,
> plus Claude if its existing feature gate is later enabled. It resolves the
> missing owner decision recorded by review trigger 3; it does **not** fabricate
> modality evidence that the application does not yet collect. Assessment
> coverage, aggregate monitoring, escalation, and provider-specific evidence
> remain incomplete below.
>
> Assembled from the implemented state of the repository. The review cadence is a
> judgement rather than a fact — the default recorded here was proposed by the
> implementer and stands until the owner replaces it. Change the row and the
> triggers together; a date without triggers is the weaker half.

## Decision

The `ai4ia-annotate-only` policy configured on the catalog model deployments is
**enabled but non-blocking**. For Azure OpenAI text chat completions, Foundry returns safety
annotations and AI4IA normalizes, persists, and displays them without refusing,
rewriting, or withholding the text. This repository does not evidence equivalent
annotation capture/persistence/display for image, video, Azure OpenAI Voice Live,
or Speech Voice Live.

The product decision is **assessment visibility, not application enforcement**.
For every new turn, AI4IA should preserve the provider's raw severity label while
also presenting the documented ordinal (`safe=0`, `low=1`, `medium=2`,
`high=3`) as an explanatory scale. Detection-style filters such as jailbreak and
indirect attack or protected material remain booleans rather than being forced
onto that severity scale. If no provider assessment exists, the user should see that coverage gap;
silence must never be rendered as "safe".

Indirect-attack assessment is explicitly enabled with `blocking: false`.
Document, library, recalled-memory, fetch, processing, and parsed-compute context
retain AI4IA's randomized nonce fences and are additionally wrapped in Azure's
provider-recognized `""" <documents> ... </documents> """` envelope. JSON request
serialization performs the escaping required by the safety service.

Claude is also outside that evidence. Black Forest Labs FLUX is too. Although
the Claude deployment retains the common
`raiPolicyName` property, Microsoft documents that Foundry does not provide
built-in content filtering for Claude at deployment time, and the Anthropic
Messages response does not carry Azure `content_filter_results`. AI4IA therefore
persists Claude turns with an explicit `unavailable` safety record and shows a
"not assessed" panel; it does not convert "no Azure verdict" into "nothing
flagged." Anthropic's provider safety
systems still apply, but they are not the per-turn Azure annotation contract this
record describes. Adding Claude is another trigger-3 scope expansion, so the
control remains incomplete pending an explicit compensating-control decision.

FLUX likewise has no Foundry deployment-time content filter. The application
therefore owns a fixed BFL `safety_tolerance=2`, permits one generated image per
call, restricts model/size/quality through the server catalog, applies entitlement
and per-turn spend bounds, and rejects oversized output. These are real
compensating controls, but FLUX responses carry no Azure annotation verdict for
AI4IA to persist or display. The owner's direction to deploy FLUX is
recorded as enablement direction, not as the still-missing modality-wide approval
needed to close this control.

## Approval

| Field | Value |
| --- | --- |
| Accountable owner | Ian Adams (repository owner; the address on the production alert action group) |
| Azure exception evidence | The deployed text-policy configuration itself — see below |
| Azure approval reference | Reported as held by the owner (guardrails-modification approval email); not stored in the repo |
| Owner policy direction | **Recorded 2026-09-03:** no AI4IA guardrail-based blocking; expose all returned assessments and explicit unavailable coverage |
| Decision scope | Azure OpenAI text/image/video/Voice Live, Speech Voice Live, Black Forest Labs image, and Claude if enabled, for named authenticated internal users |
| Provider-native behavior | Still applies. A provider may refuse or suppress output independently; AI4IA reports that outcome rather than claiming it was unblocked |
| Control status | **Owner decision complete; implementation evidence incomplete pending modality coverage, aggregate monitoring, disclosure, and escalation** |
| Next scheduled review | **2027-09-03**, or immediately on any review trigger below |
| Invalidated immediately by | Any trigger in "Review triggers" below — these do not wait for the annual date |

**What the deployed policy proves.** Azure's control plane refuses
a RAI policy that disables blocking on the abuse filters unless the subscription
holds an approved modification request. Verify against your own Foundry account
(`<foundry-account>`) at deployment time:

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

The document is structurally complete; the control is not. Completing the control requires all of the following without inventing or
backdating evidence:

1. ~~A dated accountable-owner decision that explicitly names the served
   providers and modalities.~~ **Recorded 2026-09-03 above.**
2. The Azure guardrails-modification approval reference and its applicable
   resource/modality scope, retained in the approved evidence system.
3. Modality-specific abuse cases, user disclosure, monitoring, escalation owner,
   and response procedure.
4. Evidence that annotations or equivalent safety signals are collected and
   displayed for each enabled provider/modality, including an explicit
   unavailable state where no signal exists.
5. Aggregate metadata-only safety events plus a tested alert/escalation path for
   actionable assessments. Prompt, response, image, audio, and transcript content
   remain outside general telemetry.
6. A provider/modality matrix recording assessment source, known coverage,
   compensating controls, and the non-blocking owner decision.

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

**Implemented for Azure OpenAI text chat completions.** Those annotations are no
longer discarded: verdicts returned on the single-call and multi-iteration agent
paths are normalized
(`app/api/src/ai4ia_api/safety.py`), persisted on the message, and shown in a
per-turn panel that states plainly that the displayed text was not blocked or
rewritten. Agent/tool loops retain the model-call ordinal on each returned
assessment rather than collapsing every iteration into one implied verdict.
Previously that chat-path evidence was thrown away.
`filtered: true` is always treated as notable, so a future switch to blocking would
surface rather than change behaviour silently.

**Not implemented, and needed before this can be called a controlled
non-blocking posture:**

1. **No aggregate signal.** Annotations are visible per message. Nothing counts them,
   alerts on a spike, or lets anyone answer "how often did the jailbreak filter fire
   last week?"
2. **No escalation path.** A high-severity annotation on a completion produces a UI
   note and nothing else. There is no queue, no reviewer, no notification.
3. **Modality disclosure is incomplete.** Text turns with no assessment now show
   an explicit "not assessed" notice. A turn that also generated image, video, or
   voice output does not yet carry a separate per-modality assessment/coverage
   record for that artifact.
4. **No tested moderation boundary.** The audit's premise for accepting an
   annotate-only posture was "a documented and validated replacement boundary". The
   replacement is currently visibility only.
5. **No complete modality/provider evidence.** Azure/OpenAI and BFL image, video,
   and both Voice Live providers are enabled, while Claude remains gated. This
   record has no evidence that every available safety output is normalized,
   persisted, displayed, aggregated, or escalated.

## Why this is not simply wrong

Stated fairly, since the decision may well be correct for this platform: this is a
single-tenant internal tool with named, authenticated users, and the products it is
compared against block content that legitimate technical work needs — security
research, incident write-ups, and code that trips protected-material heuristics.
Blocking filters in that setting produce refusals that look like malfunctions and
push users toward ungoverned tools, which is a worse outcome than an annotated
response.

That argument and the 2026-09-03 owner decision depend on the user population
staying small, known and internal. They stop holding the moment the platform is
exposed to a second tenant or to unauthenticated use — which is also when the
latent risk (tenant-public means application-public) stops being latent. That is
trigger 1 in "Review triggers" below, and it is why the review is trigger-driven
rather than only annual.

## Review triggers

The annual date is the *backstop*, not the control. The argument above depends on
facts that can change without anyone revisiting this page, so each of these
invalidates the decision the day it happens and requires re-approval before the
annotate-only posture continues:

| # | Trigger | Why it breaks the argument | How you would notice |
| --- | --- | --- | --- |
| 1 | A second Entra tenant is allowed, or any unauthenticated access is enabled | The whole justification is "small, known, internal, authenticated". This is also the moment the latent risk (tenant-public means application-public) stops being latent. | Startup already **refuses** when more than one tenant is allowed, so this cannot happen silently — the refusal is the notification. |
| 2 | A high-severity annotation is observed on a production completion | The premise is that filters would fire on legitimate technical work, not on genuinely harmful content. One high-severity hit is evidence the premise is wrong. | `AppEvents` in the Log Analytics workspace. **Nothing alerts on this today** — see the gap below. |
| 3 | A provider or output modality not named in the 2026-09-03 decision is enabled | The current direction covers the named text, image, video, and voice surfaces; a new provider or modality can introduce different assessment and refusal semantics. | Repository/deployment feature posture plus the provider/modality evidence matrix. |
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
