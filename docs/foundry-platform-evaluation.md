# Microsoft Foundry platform updates evaluation

> **Decision (2026-09-24): September 2026 Foundry announcements — record, do
> not activate.** This record adds no model deployment, Azure resource,
> identity, role, APIM policy, toolbox version or runtime capability. The GPT-6
> family is already in the catalog, and #511 proposes Astra's US Data Zone
> deployment. Claude Opus 5.5 is incompatible with AI4IA's thinking-disabled
> Claude profile, and no Claude model is activated yet. The Agent Service
> features target Foundry prompt and hosted agents, which AI4IA deliberately
> does not use as its runtime. The custom photo avatar requirement added on
> 2026-09-25 follows the same rule: its Phase 1 backend is implemented behind a
> default-off flag and a fail-closed Limited Access check, and nothing is enabled
> (see the [design](photo-avatars.md)).

AI4IA's FastAPI runtime owns agents, tools, approvals, receipts, memory and
scheduling. Foundry supplies model deployments behind SimpleL7Proxy → APIM,
Voice Live behind the FastAPI relay → APIM, and one toolbox MCP endpoint behind
the official-MCP APIM. A Foundry capability is adoptable only when it runs inside
those seams, or when a separately approved design moves a seam. Public
availability is not subscription entitlement or quota. A catalog or `foundry/`
change that reaches `main` runs `deploy.yml`, so model additions need explicit
owner approval before merge.

## September 2026 summary

| Announcement | Platform status | AI4IA position | Decision and next step |
| --- | --- | --- | --- |
| GPT-6 Astra, Sol and Luna | GA | Astra (Global Standard) and Sol/Luna (Global and Data Zone Standard) are catalog rows with sourced prices | #511 adds Astra's eastus2 US Data Zone row (merge provisions); swedencentral offers no Astra Data Zone yet |
| Claude Opus 5.5 | GA, Hosted on Azure | Capacity deployed in the dedicated Claude account; not cataloged. Opus 5 and Sonnet 5 are deployed but not activated | Live check confirmed disabled thinking returns 400; needs the adaptive profile, and activation waits on target-tenant admin actions |
| Voice agents in Agent Service | Public preview | Voice Live through the FastAPI relay → APIM, two providers | Not adopted; needs a new provider design |
| Voice-agent observability | Public preview | Applies only to Foundry voice agents | Not applicable |
| Custom photo avatars from a description (owner requirement, 2026-09-25) | Limited Access; creation REST surface undocumented | Phase 1 backend implemented default-off: create, status, preview, list, delete and report through an exact-operation APIM API, with a fail-closed capability check. Real-time avatar sessions are in progress | [Design](photo-avatars.md); activation waits on the Limited Access approval and RAI re-approval |
| Long-running resilience | Public preview, hosted agents | Resumable workflows on the Durable Task Scheduler worker | Not applicable |
| Agent Framework updates | Announced | No Agent Framework dependency | Not applicable |
| Foundry dev pack | Public preview | Optional operator toolchain | No repository requirement changes |
| Toolboxes for prompt agents | Public preview; GA for hosted agents | Toolbox consumed as MCP from AI4IA's runtime | No change |
| Tool search in toolboxes | GA | Live toolbox uses `toolbox_search_preview`, which lists `tool_search` and `call_tool` | #512 binds toolbox consent to the reviewed manifest; keep the preview spelling |
| Agent-to-Agent (A2A) tool | GA | Outgoing `a2a` type already modeled; unused by the canonical toolbox | No change; incoming design stays blocked |
| Routines | GA | Validation-only design artifact | Stay design-only |
| Insights in Foundry | Public preview | Content-free GenAI spans in AI4IA's Application Insights | Not adopted; needs infrastructure and cost decisions |
| Rubric evaluator, synthetic and traces-to-dataset generation, agent optimizer | GA later in September 2026 | Offline and live authored-synthetic evaluation only | Not adopted; judge and production-trace decisions pending |
| Entra and Agent 365 lifecycle enforcement | Announced | No Foundry agent identities | Not applicable |
| Network egress controls | Public preview, hosted agents | Egress enforced in the application | Not applicable |
| APIM AI Gateway tier and Admin Connected Models | Preview expected October 2026 | Existing APIM behind SimpleL7Proxy | Re-evaluate when published |
| run-assert-eval skill | Open source | Not integrated | Not adopted |
| azure-ai-projects 2.7.0 | Released 2026-09-18 | Pinned at 2.7.0 after the SDK review | Toolbox and Skills contracts unchanged; voice agents and `invoke_latest_toolbox_mcp()` not adopted |

## Models

### GPT-6 family

`gpt-6-astra` (Global Standard) and `gpt-6-sol`/`gpt-6-luna` (Global and Data
Zone Standard) already exist in `infra/models.json`, priced from Microsoft's
launch posts. The launch post also lists Standard deployment for Astra in the
US and EU Data Zones, at USD 11/55 (US) and 12/60 (EU) per million short-context
input/output tokens against 10/50 Global.

The 2026-09-23 read-only subscription observation behind #505 narrows that:

- eastus2 offers Astra 2026-09-03 as `DataZoneStandard`, with its quota counter
  at 0 of 333 used;
- swedencentral offers it only as Global Standard, with no Data Zone counter.

PR #511, open on 2026-09-24, adds the eastus2 US Data Zone row at capacity 50.
Merging it provisions that deployment, and the preprovision preflight re-checks
the counter first. Re-check the swedencentral offering before adding an EU row.

### Claude Opus 5.5

Verified on 2026-09-24:

- `claude-opus-5-5` is GA as version 2 (Hosted on Azure) and version 1 (Hosted
  on Anthropic infrastructure), with a 1M-token context and 128K output. The
  Azure-hosted version supports Global Standard and US Data Zone Standard;
  eastus2 is in the documented region table.
- USD per million tokens: 4 input, 20 output, 0.20 for cache hits (0.05x
  input), 5 for five-minute and 8 for one-hour cache writes. US Data Zone
  Standard applies the documented 1.1x multiplier.

It is not cataloged because it conflicts with the shipped external-Claude
profile (`anthropicThinking: "disabled"`, low/medium/high effort, text and the
governed function-tool loop):

1. **Adaptive thinking is always on.** `thinking: {"type": "disabled"}` or a
   manual `budget_tokens` returns HTTP 400. `build_anthropic_payload` in
   `app/api/src/ai4ia_api/gateway/anthropic.py` sends disabled thinking for every
   external-Claude profile, so every request would fail. Microsoft Learn's
   thinking/effort table still footnotes `disabled` as allowed at effort `high`
   or below for this model. Live calls on 2026-09-25 settled it: both Opus 5.5
   deployments returned HTTP 400 `"thinking.type.disabled" is not supported for
   this model` at effort `low` and `high`, and HTTP 200 with `thinking` omitted.
2. **Forced tool use returns HTTP 400.** Only `tool_choice` `auto` and `none`
   are accepted. AI4IA's agent loop already sends `auto` and strips any
   caller-supplied `tool_choice` (`agents/runtime.py`), but
   `_tool_choice_to_anthropic` would still translate an explicit `required` or
   named choice to the rejected `any` and `tool` types.
3. **Thinking blocks bind to the conversation.** They must be passed back
   unmodified in tool loops. A replay after any system, tool or earlier-message
   change returns 400 by default for accounts created on or after 2026-08-31.
   Both of the adapter's response parsers drop thinking blocks today, so the
   first tool continuation would fail. Progress text between tool calls also
   arrives in thinking blocks, which are empty at the default display setting.

The Opus 5.5 capacity is deployed in the dedicated account (below) but stays out
of the catalog until the adaptive-thinking profile exists. A catalog row now
would have to declare the thinking-disabled profile the model rejects.

#### Current Claude status

AI4IA does not serve Claude yet (2026-09-25). The integration shipped
default-off in #493: `AI4IA_CLAUDE_ENABLED` is `false`, the external staging
flag and binding are unset, and APIM keeps its disabled Claude policy. Tenant
access is not the blocker: operators reach both tenants through isolated
per-tenant Azure CLI profiles, the pattern
`AI4IA_CLAUDE_TARGET_AZURE_CONFIG_DIR` already expects.

Done on 2026-09-25, through the approved operator units:

- **Dedicated target account.** `infra/claude-target.bicep` provisioned a
  keyless (`disableLocalAuth`, `public-keyless`) account holding Opus 5
  DataZoneStandard 40, Sonnet 5 GlobalStandard 80 and DataZoneStandard 80, and
  Opus 5.5 GlobalStandard 40 and DataZoneStandard 40. Every deployment is
  version 2, `NoAutoUpgrade`, and uses its full quota counter.
  - A separately owned learning deployment holds the entire Opus 5
    GlobalStandard counter, so the catalog serves Opus 5 as US DataZoneStandard
    only.
  - The terms attestation matches the one already on record for the target
    subscription, not the repository variables.
- **Live model check.** With temporary operator access, the adapter's exact
  payload (thinking disabled, effort `low`) returned HTTP 200 from Opus 5 DZ and
  from Sonnet 5 GS and DZ. Opus 5.5 behaved as recorded above.
- **Source identity.** `infra/claude-identity.bicep` created the dedicated
  UAMI. A multitenant application in the source tenant has no password or key
  credential, and exactly one federated credential for that UAMI.

What still blocks activation:

- **Target service principal.** Creating the application's service principal
  in the target tenant is refused for a non-admin: Graph allows it for a foreign
  multitenant app only to an Application Administrator or Cloud Application
  Administrator in that tenant. A target-tenant admin can create it, or grant
  admin consent for the application.
- **Inference role (fixed in the access unit).** The documented MaaS-only
  custom role (`Microsoft.CognitiveServices/accounts/MaaS/*`), which
  `infra/claude-access.bicep` originally created and the binding readback
  required, did not authorize Claude Messages: calls still returned HTTP 401
  `Principal does not have access to API/Operation` after 15 minutes. The
  provider's registered operations contain no `MaaS` data action at all. A
  throwaway service principal that never held a broader role then got 401 for
  14 minutes with `AIServices/endpoints/invoke/action` alone. It got HTTP 200
  within about five minutes once `Microsoft.CognitiveServices/accounts/AIServices/*`
  was added. The access unit and `INFERENCE_ACTIONS` now require exactly
  `AIServices/*`, which excludes OpenAI, Speech and every other Cognitive
  Services surface; the test principal was deleted. Data-plane authorization
  also outlived role removal by more than 55 minutes, even for a newly issued
  token, while ARM already reported no assignment and no effective data action.
  Test any narrower candidate with a principal that never held a broader role,
  and roll back by disabling dispatch rather than by revoking the grant.
- **CI readbacks.** The binding readbacks read the Entra application, its
  federated credential and the target service principal as app identities.
  That needs admin-consented `Application.Read.All` for the deploy identity in
  the source tenant and for a new target reader in the target tenant. Obtain
  it, or approve a reviewed contract change in which CI verifies the account,
  deployments, role and APIM routes, and an operator verifies the Entra records
  at activation and after any binding change.

Alternatives that avoid target-tenant directory admin are recorded here but not
adopted, since each changes the approved design:

- **Same-tenant Claude.** The source subscription also offers these models with
  full quota. A dedicated account there would authenticate APIM with its managed
  identity and need no application, federation or Graph readback.
- **Target-side hop.** A proxy or APIM in the target subscription would use its
  own managed identity for Claude. It would validate a source-issued
  managed-identity token from AI4IA's APIM, so no secret crosses tenants.

The cross-tenant path must never use a client secret.

#### Adaptive-thinking profile design

The live check on 2026-09-25 settled the contradiction: Opus 5.5 rejects
disabled thinking. The design below therefore applies. It needs an owner
decision to amend the thinking-disabled rule in `AGENTS.md`. A first,
text-only stage is smaller: omitting `thinking` already returns HTTP 200, and a
profile without tool calling never continues a tool loop, so it needs no block
replay. The full design reuses the agent loop's existing turn-local
continuation channel rather than adding storage:

- **Catalog.** `anthropicThinking` gains `"adaptive"` in
  `infra/models.schema.json` and `ModelEntry`.
  - `require_external_profile` accepts, for adaptive rows only, effort `low`
    to `xhigh` with default `medium`. Learn documents `xhigh` and `max` as
    equivalent, and AI4IA's effort vocabulary stops at `xhigh`.
  - Ordinary rows still omit the field, preserving legacy consent and
    publication digests.
- **Payload.**
  - Omit `thinking` (equivalent to adaptive), send `output_config.effort`, and
    keep the default `display: "omitted"`.
  - Refuse a `required` or named `tool_choice` before dispatch.
  - Size `max_tokens` from the profile rather than the adapter's generic 4,096
    default, because thinking counts against it. A `max_tokens` stop keeps
    mapping to the incomplete outcome.
- **Capture.** `anthropic_json_to_chat` and `parse_anthropic_event` keep a
  tool-use response's ordered content blocks as opaque provider continuation
  items.
  - Kept blocks: `thinking` and `redacted_thinking` with their `signature`,
    plus `text` and `tool_use`.
  - The stream parser assembles `thinking_delta` and `signature_delta`
    fragments; no thinking delta reaches the browser.
- **Replay.** `call_model` in `agents/runtime.py` already returns provider
  continuation items. The loop stores them on the in-turn assistant message
  under `RESPONSES_OUTPUT_ITEMS_KEY` and in `CheckpointResponse.outputItems`.
  `TurnCheckpoint` restores them after approval pauses and durable workflow
  resume. `messages_to_anthropic` replays those blocks byte-for-byte for the
  assistant tool-call message instead of rebuilding it from `content` and
  `tool_calls`.
- **History and evidence.**
  - Continuation items never enter session message history. Later turns omit
    earlier thinking blocks, which the API allows, and model switches between
    turns stay safe.
  - Receipts, SSE events and logs strip them exactly as they strip Responses
    `output_items`. At most a receipt records that opaque continuation state
    existed, and nothing is labeled as reasoning (see
    [execution receipts](architecture.md#execution-receipts-not-hidden-reasoning)).
- **Prefix pinning.**
  - `call_model` re-runs `bound_agent_context` before every round and can
    evict older turns as a turn grows. For an adaptive turn, eviction is
    decided at the first round and then frozen.
  - A turn that would need further eviction fails closed with no retry. So
    does a resume whose pinned prompt or tool schema no longer matches.
  - Execution-time denials stay appended tool results, never schema edits.
  - The beta `thinking-binding-controls-2026-08-01` header's `drop_block`
    behavior stays unused.
- **Pricing and refusals.**
  - `tokenRatesBySku` gains Opus 5.5 rates: Global 4/20 with 0.20 cache reads,
    and US Data Zone 4.4/22 with 0.22.
  - Thinking tokens bill as output tokens and arrive in
    `usage.output_tokens`. Cache writes stay cost-unknown, and the adapter
    still requests no caching.
  - `stop_reason: "refusal"` maps to an explicit refused outcome with no retry
    instead of passing through as an unknown finish reason.
- **Controls.**
  - Payload shape and forced-choice refusal.
  - Byte-identical capture and replay on both transports.
  - Approval-pause resume.
  - Receipt, event and log stripping, with a mutation that removes the strip.
  - Fail-closed prefix changes.
  - An end-to-end loop against a fake provider that enforces Opus 5.5's 400
    rules. Its paired control removes the replay and observes the 400.
- **Unchanged.** The existing
  [Claude source contract](runbooks/feature-enablement.md#cross-tenant-claude-source-contract)
  still governs the target, binding and quota readbacks. Claude still cannot
  obtain attempts-v1 or finite-dollar admission.

## Voice

Voice agents are a new Agent Service agent type for prompt and hosted agents.
azure-ai-projects 2.7.0 adds preview `.beta.voice_agents` realtime,
conversation and telephony clients. Microsoft recommends voice agents for new
enterprise voice workloads while the Voice Live API remains supported.

AI4IA keeps the FastAPI relay → APIM → Voice Live path with the `azure_openai`
and `speech_voice_live` providers (see the
[realtime and voice lifecycle](architecture.md#realtime-and-voice-lifecycle)).
A voice agent would own persona, tools and knowledge inside Foundry. Adoption
needs a separate APIM WebSocket API and scoped key (never a direct Foundry URL),
a provider adapter and catalog entry, consent, receipt and accounting mappings,
exclusion of telephony and channel publishing, and
[review trigger 3](rai-decision-record.md#review-triggers) for a new provider.
Voice-agent observability covers only Foundry voice agents.

### Custom photo avatars

The owner added this requirement on 2026-09-25: generate photo avatars from a
text description, and talk to them. It was verified in code against test
resources:

- **Creation** takes about 30-45 seconds and produces a 1024×1024 portrait.
- **Batch talking-head video** takes about 20 seconds for a 10-second clip.
- **Voice Live** accepts a custom photo avatar and negotiates WebRTC. The media
  stream itself is not tested yet.

The creation REST surface is the Foundry portal's own endpoint, and it isn't
publicly documented. Custom text to speech avatar is Limited Access, and AI4IA's
registration is pending.

The Phase 1 backend is now implemented behind a default-off flag: create,
status, preview, list, delete and report, through an exact-operation APIM API,
with a fail-closed capability check. The Speech Voice Live relay still rebuilds
`session.update` and drops any client `avatar` field, and the web voice client
still uses only WebSocket audio; real-time avatar sessions are the next phase.
The [photo avatar design](photo-avatars.md) phases the work:

1. decisions and spikes;
2. create, preview, list and delete;
3. real-time conversation;
4. optionally, rendered videos.

It needs two owner-approved exceptions to the gateway rule:

- WebRTC media that flows directly between the browser and Microsoft's media relay;
- a bounded fetch of provider-issued artifact links.

It also needs re-approval under
[review trigger 3](rai-decision-record.md#review-triggers).

## Long-running work and developer tooling

- **Long-running resilience** keeps a hosted agent's response alive across
  client disconnects and host restarts. AI4IA has no hosted agent. Its
  resumable workflows run on the existing Durable Task Scheduler worker with
  owner CAS checkpoints (see [workflow automation](workflow-automation.md) and
  [durable workflow execution](architecture.md#durable-workflow-execution)).
- **Agent Framework** adds workflow checkpointing, Agent Channel, AG-UI,
  CodeAct with Hyperlight containers and episodic procedural memory. AI4IA's
  agent loop, memory and code execution are application-owned, so adopting any
  of these is a runtime redesign, not a dependency upgrade.
- **The Foundry dev pack** installs the Azure CLI, azd with its Microsoft Foundry
  extension and the Microsoft Foundry skill, plus Foundry Toolkit or Foundry
  Canvas (preview) when VS Code or the GitHub Copilot app is present. It does not
  change the repository's deployment prerequisites. Its skill and canvas guide
  hosted-agent creation and deployment; they do not override the rules in
  [deploying with an agent](deploy-with-an-agent.md).

## Tools and knowledge

### Toolboxes

Toolboxes are GA for hosted agents and preview for prompt agents. AI4IA is
neither: its runtime consumes the toolbox MCP endpoint through the official-MCP
APIM (see [Foundry toolbox](foundry-toolbox.md)). Current Microsoft Learn
consumer examples call that endpoint without the
`Foundry-Features: Toolboxes=V1Preview` header; skill operations still send
`Skills=V1Preview`. APIM keeps injecting both flags until a bounded read-only
probe shows that dropping the toolbox flag leaves `initialize`, `tools/list` and
`resources/list` unchanged. Changing the header is a catalog plus APIM rollout.

### Tool search

The GA contract is `{"type": "toolbox_search"}`. It hides every unpinned toolbox
tool from `tools/list`. The platform then injects a BM25-backed `tool_search`
meta-tool and a generic `call_tool` that invokes any discovered tool by name.
For AI4IA:

- consent, auto-approval and execution-time re-checks bind a tool name and its
  contract digest. `call_tool` is documented as a generic name-plus-arguments
  dispatcher, so its digest need not change when the hidden tool set changes:
  consent could silently cover tools added to later toolbox versions, and
  re-checks would see `call_tool` rather than the target tool;
- with two real tools, tool search saves little context.

The canonical manifest keeps `toolbox_search_preview`. The preview spelling
already behaves this way: after the first reconciliation on 2026-07-30, the live
`tools/list` returned exactly `tool_search` and `call_tool`. The consent gap was
therefore live, not only a risk of switching spellings.

PR #512, open on 2026-09-24, closes the consent half. The MCP catalog generator
records a digest of the reviewed toolbox manifest in the toolbox server's
configuration revision, so any manifest change renews consents and pending
approvals once it deploys. Two parts remain:

- execution-time re-checks still see `call_tool` rather than the dispatched
  tool, although per-invocation approval shows the target in the arguments;
- the digest binds reviewed source, not out-of-band edits to the live toolbox,
  which the next reconciliation replaces.

Removing tool search from the two-tool toolbox would list its real tools
directly, each with its own contract, but would change the tool names agents
have attached; that is an owner choice. Switching to the GA spelling changes
nothing AI4IA relies on, but should wait for a read-only listing probe against
a candidate version. SDK 2.7.0 still ships `ToolboxSearchPreviewToolboxTool`;
its removal would force the switch.

The greenfield standup expected `toolCount: 3`, apparently from the manifest's
three entries. The recorded listing and the GA documentation both give two
listed tools, so the
[standup acceptance check](runbooks/greenfield-standup.md) now expects
`toolCount: 2`.

### Agent-to-Agent

The GA A2A tool is the outgoing direction: a toolbox `a2a` tool with
`a2aVersion` `"1.0"`, modeled since SDK 2.5.0 alongside `a2a_preview`. Incoming
A2A exposes a deployed Foundry prompt agent that uses the responses protocol;
protocol 1.0 is GA and 0.3 is preview. AI4IA has no Foundry prompt agent, so
all seven blockers in `foundry/a2a/example.a2a.json` remain. Protocol 1.0 is the
target when an incoming design is approved.

### Routines

GA routines start one existing Foundry agent (`invoke_agent_responses_api`) from
a one-time timer, a recurring schedule, or an event (GitHub issues and Teams
channel messages at launch). Each routine runs with the creator's or the agent's
identity through the `authorization` argument added in SDK 2.6.0. The Python
surface is still `project.beta.routines.create_or_update` in SDK 2.6.1 and
2.7.0. AI4IA's artifact stays design-only: there is no Foundry agent to target,
and a routine run would bypass owner admission, consent, receipts, hard-quota
admission and schedule-slot contracts. Governed scheduling is AI4IA's default-off
`AI4IA_WORKFLOW_SCHEDULING_ENABLED` path. The reminder tool remains
hosted-agent only.

## Observability and optimization

- **Insights in Foundry** analyzes agent traces in the Application Insights
  resource connected to a Foundry project. Findings depend on agent identity,
  version, spans and content, and scans incur model and telemetry costs.
  AI4IA's GenAI spans are content-free by contract (see
  [content-free GenAI model spans](runbooks/telemetry.md#content-free-genai-model-spans)),
  and the repository declares no Application Insights connection on a Foundry
  project. Adoption needs an approved infrastructure change and cost decision;
  any content-dependent finding also needs the `AppGenAIContent` decision.
- **Rubric evaluators, synthetic and traces-to-dataset generation, and the agent
  optimizer** introduce paid judges, production-trace datasets and generated
  candidates. The offline program refuses live calls, judges and production
  content, and the live authored-synthetic driver has its own gates (see
  [reports, variance and remaining decisions](behavioral-evaluations.md#reports-variance-and-remaining-decisions)).
  Adoption needs those judge and production-trace decisions. Optimizer output
  would enter normal review through the catalog and reviewed publication; it is
  never applied automatically.

## Governance

- **Entra and Agent 365 lifecycle operations** are enforced by the Foundry
  runtime for Foundry agents. AI4IA's workloads use Entra app registrations and
  managed identities, and its own auth and group-policy layers govern users.
- **Network egress controls** govern hosted-agent sandbox egress. AI4IA enforces
  egress at execution time (SSRF, public-HTTPS and DNS pinning checks). Toolbox
  code interpreter egress is configurable through the manifest's
  `container.networkPolicy`.
- **AI Gateway tier and Admin Connected Models** reach preview in October 2026;
  existing APIM tiers stay supported. AI4IA already centralizes model access in
  APIM (Basic v2) behind SimpleL7Proxy. When published, evaluate tier and
  networking (Basic v2 has no outbound VNet integration), generated policy and
  fragment limits, cost, and how Admin Connected Models map onto the catalog.
  Any change needs an infrastructure what-if and owner approval.
- **run-assert-eval** chains Clarity, ASSERT and the Agent Control
  Specification. It is not integrated; its evaluation runs need the same judge
  and live-call approvals.

## SDK 2.7.0 upgrade impact

azure-ai-projects 2.7.0 (2026-09-18) adds preview Voice Agent clients,
prompt-agent `harness` and `skills`, `invoke_latest_toolbox_mcp()`, toolbox
version metadata and RAI invocation moderation. `ToolboxObject` now requires
`updated_at` and `versions` constructor arguments. The hash-verified wheel exports
17 classes named `*ToolboxTool`: the 16 already reviewed plus
`VoiceAgentToolboxTool`. That class is a `VoiceAgentTool` with discriminator
`toolbox` that attaches a toolbox to a voice agent; it is neither a
`ToolboxTool` subclass nor a manifest tool type.

Run against that wheel, the previous name-based reflection gate in
`app/api/tests/test_foundry_toolbox.py` failed only on `VoiceAgentToolboxTool`.
Reflection now follows the SDK's `ToolboxTool` hierarchy, which selects the same
16 classes on 2.6.1 and 2.7.0 and passes on both. Paired controls prove that a
same-named class outside the hierarchy is ignored and that a `ToolboxTool`
subclass fails parity whatever its name. The exact-pin, manifest-version and
wheel/source review then moved the pin to 2.7.0; its findings are in the
[toolbox runbook](foundry-toolbox.md#deliberately-unsupported-sdk-toolbox-types).
Nothing else in this evaluation depends on 2.7.0.

## Sources

Checked 2026-09-24:

- [September 2026 Foundry announcement](https://azure.microsoft.com/en-us/blog/ship-agents-faster-with-expanded-model-choice-voice-agents-and-continuous-optimization/)
- [GPT-6 Astra, Sol and Luna launch pricing](https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/)
- [Claude Opus 5.5 in Foundry](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/claude-opus-5-5-comes-to-microsoft-foundry-for-long-running-coding-and-knowledge/4558051)
- [Claude models in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/claude-models)
- [What's new in Claude Opus 5.5](https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5)
- [Claude pricing and Foundry US Data Zone multiplier](https://platform.claude.com/docs/en/about-claude/pricing)
- [Voice agents in Foundry](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/introducing-voice-agents-in-microsoft-foundry/4557276)
- [Long-running agent resilience](https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience)
- [Microsoft Agent Framework updates](https://devblogs.microsoft.com/agent-framework/interactive-experiences-memory-and-resilient-execution/)
- [Foundry Dev Pack](https://devblogs.microsoft.com/foundry/foundry-devpack-announcement/)
- [Create and manage a toolbox](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox)
- [Tool search in a toolbox](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/tool-search)
- [Connect to an A2A endpoint](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent)
- [Enable incoming A2A](https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint)
- [Routines GA](https://devblogs.microsoft.com/foundry/from-chatbots-to-automated-assistants-routines-in-microsoft-foundry-are-now-generally-available/)
- [Insights in Foundry](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/insights-in-foundry-turns-agent-traces-into-action/4559634)
- [azure-ai-projects 2.7.0 release](https://pypi.org/project/azure-ai-projects/2.7.0/)

The photo avatar sources, checked 2026-09-25, are listed in
[the plan](photo-avatars.md#sources).
