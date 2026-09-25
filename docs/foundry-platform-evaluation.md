# Microsoft Foundry platform updates evaluation

> **Decision (2026-09-24): September 2026 Foundry announcements — record, do
> not activate.** This record adds no model deployment, Azure resource,
> identity, role, APIM policy, toolbox version or runtime capability. The GPT-6
> family is already in the catalog, and #511 proposes Astra's US Data Zone
> deployment. Claude Opus 5.5 is incompatible with AI4IA's thinking-disabled
> Claude profile, and no Claude model is activated yet. The Agent Service
> features target Foundry prompt and hosted agents, which AI4IA deliberately
> does not use as its runtime.

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
| Claude Opus 5.5 | GA, Hosted on Azure | Not cataloged; Opus 5 and Sonnet 5 are not activated either | Blocked on Claude activation, then on a live check of disabled thinking; adaptive design recorded below |
| Voice agents in Agent Service | Public preview | Voice Live through the FastAPI relay → APIM, two providers | Not adopted; needs a new provider design |
| Voice-agent observability | Public preview | Applies only to Foundry voice agents | Not applicable |
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
| azure-ai-projects 2.7.0 | Released 2026-09-18 | Pinned at 2.6.1 | Reflection gate now ignores the non-toolbox `VoiceAgentToolboxTool`; upgrade through the normal SDK review |

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
   or below for this model, contradicting the same page's model table and
   Anthropic's migration notes. Contradictory evidence refuses; it is not a
   reason to guess.
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

Do not add a row, including one with `runtimeEnabled: false`: runtime
disablement still keeps the desired external deployment inventory.

#### Current Claude status

Neither Claude Opus 5 nor Sonnet 5 is deployed or available (checked
2026-09-24). The integration shipped default-off in #493: the
`AI4IA_CLAUDE_ENABLED` repository variable is `false`, the external staging flag
and binding are unset, and APIM keeps its disabled Claude policy. The last
read-only observation of the intended target account, on 2026-09-20, found no
Claude deployments. No dedicated account, identity or grant has been created.
Activation still waits on three owner decisions from the
[separate approved operator units](runbooks/feature-enablement.md#separate-approved-operator-units):

- the network mode;
- the legal entity that accepts Anthropic's terms for the target subscription;
- the dedicated account, identity and access grants.

Opus 5.5 work follows that activation.

#### Adaptive-thinking profile design

Settle the contradiction first. Once Claude is active, a separately approved
Opus 5.5 canary sends the existing thinking-disabled payload at effort `low`.
HTTP 200 means the current profile applies, so Opus 5.5 needs only a catalog
row, prices and the normal external readbacks. HTTP 400 confirms Anthropic's
contract and selects the design below. It needs an owner decision to amend the
thinking-disabled rule in `AGENTS.md`. It reuses the agent loop's existing
turn-local continuation channel rather than adding storage:

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
subclass fails parity whatever its name. The SDK stays pinned at 2.6.1; the
upgrade still needs its exact-pin, manifest-version and wheel/source review.
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
