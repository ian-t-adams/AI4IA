# AI4IA User Guide

AI4IA is a governed chat, agent, and document workbench. Use it to talk to
multiple models, run agent tools, work with documents and media, generate
artifacts, and review usage. The API enforces identity, feature gates, ownership,
and tool safety; the web app is the user interface.

## Start

1. Open the web app URL for the environment.
2. Sign in with Entra when prompted. Local/dev environments may use a configured
   dev identity instead.
3. Start a chat session or reopen an existing session from the sidebar.
4. Open the Conversation Inspector. It has three tabs — **Setup** (model,
   instructions, agent and tools, Voice Live), **Context** (documents and
   memory), and **Usage** — and each tab's sections expand one at a time. The
   app clamps model parameters, including reasoning effort, to catalog limits.

## Chat well

- Put goals and constraints in the message. Mention files, models, regions, or
  customer context when they matter.
- Use attachments for one-off context in the current session.
- Use the document library for reusable material that should be searchable across
  sessions.
- Treat cited document snippets and tool output as grounded context, not as
  permission to skip review.
- Temperature and Top P currently remain visible even when the selected model does
  not support them. The UI bounds Temperature to 0-2 and Top P to 0-1; for compatible
  models, the API forwards those values unchanged. The UI and API cap output tokens
  to the selected catalog model's published maximum. For GPT-5 and o-series
  deployments, the gateway strips unsupported sampling fields and translates the
  output-token field to the model's accepted shape. Reasoning effort is offered
  only from the `reasoningEffort` list in `infra/models.json`; unsupported values
  are treated as provider errors, not retried as capacity failures. Control
  visibility does not grant a model capability or override server policy.

## Agents and workflows

- Use built-in agents for common jobs.
- Create a user agent when you repeatedly need a specific role, instruction set,
  model, or tool bundle.
- Use workflows for repeatable multi-step work. Workflows run through the same
  governed tool path as chat, and each step is offered the same capabilities a
  chat turn gets: reading your document library, Web IQ search and browsing, and
  — when switched on for that step — recalling and saving memories.
- The workflow editor has two tabs. **Build** is where you name the workflow and
  order its steps; **Run & test** is where you run one and read the result. The
  result stays in the panel — a per-step trace showing which steps succeeded,
  which one failed, and which never started — so you can adjust a step and run
  again without losing the output. Use **Open in chat** when you want the run's
  conversation.
- **Each step lists what it can actually do.** A step's tool surface is not the
  same as its agent's tool list, so the editor states the difference per step:
  document reading is always on, web search is offered whenever the deployment
  has Web IQ configured, the two memory tools appear only when switched on for
  that step, and anything that returns a chat attachment (image and video
  generation, document processing, MCP tools) is marked **chat only** because a
  workflow step has no way to deliver one.
- **Tools are switched on per step, under "Tools for this step".** They are added
  on top of whatever the step's agent already carries — never instead of them.
  This is how you give a capability to one of the built-in agents, whose own tool
  lists you cannot edit. Only the tools that genuinely work inside a workflow step
  are offered, so there is no checkbox that saves and then does nothing.
- **Memory has to be switched on, and a step without it will not tell you.**
  `remember_memory` saves a short durable fact; `recall_memory` reads them back.
  A step with neither cannot write to your memory — and, importantly, the model is
  not told that it lacks the tool, so it will often reply as though it saved your
  notes and the run will still be recorded as successful. Tick **Save memory**
  under **Tools for this step**; the step's capability list flags it until you do.
  Saves are then reported honestly: a fact already covered by an existing memory
  is reported as "nothing new stored", not as a save.
- **A run can be scoped to specific documents.** Under **Documents**, select the
  library documents the run should be restricted to. Select nothing and every
  step can read any of your ready documents.
- When the deployment provides durable execution, the workflow runner offers
  **Keep running if the app restarts**. Leave it off for quick runs: the reply
  comes back in the request as usual. Turn it on for long multi-step work, and
  the run is handed to an orchestration that survives a restart, scale-in, or
  crash — the runner then waits for it to finish, because the reply is written
  when the run completes rather than held open in the request. If it is still
  going after two minutes the page stops waiting and says so; the run is *not*
  cancelled, and its reply appears in the run's chat when it finishes. The option
  is hidden entirely on deployments that cannot honour it.
- Attach only the tools an agent needs. Tool output is metered, logged, bounded,
  and redacted where applicable.
- A selected agent is the standing conversation persona. An explicit `@agent`
  mention remains a one-turn override. The inspector shows the resolved
  instruction stack read-only, including whether an inherited agent prompt came
  from the curated catalog or from one of your user agents; conversation-level
  instructions remain the editable layer. It also shows inherited and
  conversation-level tool changes; the API authorizes every call again at execution.
- Tool rows state whether a capability is available in typed chat, Voice Live, or
  both. Typed-only tools are never silently advertised to Voice Live.

### Agent activity

Tool-using turns show live activity such as searching, reading, running a tool,
being blocked, or encountering an error. Completed turns retain a collapsed,
activity list. The required contract is an execution trace, **not**
chain-of-thought: it may contain only the step type, validated tool alias/name,
fixed reason category, and coarse outcome—never hidden reasoning,
credentials, arguments/results, prompts or queries, audio, or transcripts.

## Documents and media

The composer has one **Attach** action for documents, images, audio, and video.
The API advertises and enforces the actual type, size, count, modality, and ingest
path limits. Uploads are queued sequentially, show progress/failure, and can be
retried or dismissed.
The Attach control remains disabled until those capabilities load. An uploaded
library document appears as selected context only after the session association
succeeds. Active uploads temporarily block conversation navigation so a late
completion cannot attach to or appear in another conversation.
AI4IA has two storage/context paths:

- **Session attachments** add bounded text to the current chat only.
- **Document library** uploads a reusable, user-owned document, enriches it,
  indexes chunks for retrieval, and makes it available to library tools.

Library documents move through ingest states. Only `ready` documents contribute to
RAG, media deep-links, save-to-memory, sharing, `fetch_document`, `run_code`,
`export_document`, or `process_document`.

Sharing is tenant-scoped:

- `private` means owner-only.
- `shared` grants read access by email.
- `public` means tenant-authenticated users can read it. It is not an anonymous
  internet link.

Owned and shared documents can be explicitly selected for a conversation. A missing
selection retains legacy all-accessible behavior, an explicit empty selection disables
library context, and a non-empty selection is an exact allowlist. Revoking a share
removes that document from effective retrieval and tools immediately, even if its
stale id remains in an older session record.

## Voice

- Turn-based transcription and text-to-speech use compatible HTTP calls on the
  normal `FastAPI -> SimpleL7Proxy -> APIM -> Foundry` path. The **Play** control
  synthesizes an assistant message independently of any Voice Live connection.
- Voice Live uses a browser WebSocket directly to the API ingress because the
  Next.js proxy does not proxy WebSockets.
- The API still enforces auth, Origin checks, entitlements, metering, deployment
  selection, and optional governed tools.
- The orange live microphone starts and stops Voice Live inside the current chat.
  The normal transcript and composer stay available, and finalized spoken turns
  are saved into that same session.
- Open **Voice** under the Conversation Inspector's **Setup** tab to choose the
  provider, provider model, voice, locale, temperature, turn detection,
  transcription, noise/echo, and interruption behavior. Settings apply to the
  next connection.
- Voice has no separate instructions field. The selected agent persona is
  authoritative; otherwise the saved conversation system prompt is injected by
  the API for both providers.
- Two providers are available when an operator enables both: **Azure OpenAI**
  (the default, with a catalog realtime model and its usual voice/turn-detection
  options) and **Azure Speech** (a second, opt-in provider with six curated
  `eastus2` / stable `2026-04-10` managed models). `gpt-realtime` (the Speech
  default) and `gpt-realtime-mini` are native audio with GPT-4o Transcribe;
  `gpt-4.1`, `gpt-4.1-mini`, `gpt-5-mini`, and `gpt-5.1` use the Azure Speech
  chain and Azure Speech transcription. Speech also offers curated built-in
  voices, locale, noise suppression, echo cancellation, and turn detection;
  there is no custom endpoint, lexicon, personal voice, or free-text model.
- Changing the provider (or any other voice setting) applies starting with the
  **next** connection, not the current one; it never triggers a silent reconnect
  mid-session. The chat transcript and session are shared across both providers,
  so switching providers keeps the same conversation. Azure OpenAI and Speech
  retain separate model/settings selections. Existing v2 browser preferences are
  migrated to v3; the new Speech model choice defaults to `gpt-realtime`.
- You can type while Voice Live is connected. Typed turns are saved immediately
  in the shared transcript; because an open realtime socket cannot be reseeded,
  they become Voice Live context the next time it connects.
- Starting Voice Live in an empty chat does not create a chat record until a real
  finalized voice turn needs saving. Denied microphone access or a gateway failure
  leaves the session list unchanged; use the inline **Retry** action after fixing
  connectivity. This holds for both providers.
- **Stop** tears down capture and the socket independently of transcript
  persistence. If saving finalized voice turns fails, use **Retry** or **Discard**;
  a persistence error does not keep the microphone live or trap navigation.
- If the browser reports that the microphone track ended/became muted, or its audio
  processing context cannot recover, Voice Live closes completely and asks you to
  reconnect instead of remaining silently "live."

If Voice Live controls are hidden, the feature is disabled for that environment. If
only the provider selector is hidden, only the default provider is configured.

## Generated artifacts

Image, video, document-processing, and export tools return durable artifacts when
the relevant feature and storage are configured. Artifacts are served through
authenticated API routes rather than public blob URLs.

## Memory

When memory is enabled, AI4IA can recall prior user context and can save ready
document summaries to memory. The inspector lists only memories owned by the
current user. You can create a memory, edit it inline, or delete it after an
item-labelled confirmation. Pending, conflict, error, and retry states stay on
the affected item rather than disabling the whole inspector.

Memories you create or edit are locked against automatic consolidation. `/forget`
removes this conversation's memories by default; `/forget me` removes all active
memories for your profile. Document deletion also fences and removes memories
derived from that document. There is no global consent/toggle or per-answer
recalled-memory indicator yet.

The Usage section reports known token/cost subtotals and request coverage when some
providers omit usage. `Unknown` is shown instead of zero when every request is
unknown. Prompt pressure refers only to the latest metered turn and is unavailable
when that turn used a different model or lacks prompt-token metadata.

## Custom tools and web search

Custom MCP servers and Web IQ search tools are feature-gated. When enabled:

- MCP server credentials are stored in Key Vault outside local development.
- Remote MCP endpoints pass an SSRF guard before discovery and each use.
- Web IQ contributes five server-side tools — `web_search`, `news_search`,
  `video_search`, `image_search`, and `browse_url` — and their output is bounded
  and treated as untrusted model context.

Some tool calls are held until you approve them. You will see a card naming the
tool, where the call is going, and the arguments it would send; the reply arrives
normally and the call runs only after you approve *that* card. Approving one call
does not approve the next one, and the approval expires in ten minutes.

`browse_url` (fetching a web page) and `run_code` (running code over one of your
documents) ask every time, because the model picks the address or the program.
Web searches, image and video generation, and saving to memory ask only when the
turn also contained something the assistant read on your behalf — a document you
uploaded, a saved memory, or an earlier tool result — since that is the case where
the request may not have come from you. An ordinary search or "remember that I
prefer X" is not interrupted.

If you did not ask for what the card describes, do not approve it: a document can
contain text written to make the assistant act on the author's behalf rather than
yours, and the card is where that becomes visible.

If the controls are hidden, the feature is disabled for that environment.

## Admin views

Admins can open usage, analytics, and resource dashboards. The API enforces admin
access on every endpoint; the web app only hides the navigation for non-admin users.

Usage and analytics panels are read-only over a selectable day window (default 30,
maximum 90):

- **Tokens by model** and **Tokens by day** — token consumption broken down by model
  and over time.
- **Top users** — highest-volume users by requests, tokens, and estimated cost.
- **Agents in use** — per-agent request counts, with errored and cancelled requests
  called out so failing agents are easy to spot.
- **Who uses which agents** — a user-by-agent cross-tab.
- **Requests by region**, **Requests by data zone**, and **Requests by deployment** —
  where traffic actually lands across the catalog.
- **Request status mix** — completed versus cancelled versus errored requests.

Each analytics panel is computed from a single bounded scan of usage records and
flags when a window was truncated, so large tenants stay responsive. User identities
are shown as stable internal identifiers; see the troubleshooting note below.

Platform resources shows live Azure Monitor metrics for the deployment's Container
App (requests, response time, replicas, restarts), Cosmos DB, and Azure AI Search.
Each tile degrades to unavailable when its Azure resource id, the
`azure-monitor-query` SDK, or the API identity's Monitoring Reader permission is
missing; a `—` cell means no data for that metric, not an error.
The whole Cosmos panel uses the common one-hour grain required by
`ServiceAvailability`. Metrics with incompatible aggregations are split into
separate calls so one unsupported combination cannot invalidate the whole panel.

Operations and Security panels query the existing Log Analytics workspace with
fixed bounded KQL. Every panel names its source, source timestamp, lag, and
`ok`/`partial`/`stale`/`unavailable` state. Request and dependency panels include
p50/p95/p99 latency where Application Insights data exists. Voice, tools/MCP,
documents, memory, usage coverage, and governance blocks are metadata-only.
Exact SimpleL7Proxy queue/fairness/circuit-breaker metrics remain unsupported unless
the current proxy exports stable queryable events.

## Data boundaries

- User identity is normalized at the API boundary.
- Sessions, messages, usage, agents, workflows, MCP server records, and document
  manifests are canonical in Cosmos DB.
- Derived memory vectors, search indexes, chunks, parsed artifacts, and media
  sidecars can be rebuilt from canonical records and blob storage.
- Model calls route through the configured gateway unless a native Azure service
  control plane is required.

## Known gaps

- Custom analyzer authoring, folder-level sharing, and anonymous public links are
  not implemented.
- Memory has no global user-facing enable/disable preference.
- Some proxy/APIM/provider stage percentiles and quota forecasts remain unavailable
  until the current telemetry sources expose stable dimensions; the admin UI labels
  those states rather than fabricating zeroes.

## Troubleshooting

| Symptom | What it usually means |
| --- | --- |
| Feature controls are missing | The web feature flag is off or the API feature is disabled. |
| A library route returns disabled/not found | Document understanding is not enabled or prerequisites failed startup validation. |
| A document does not appear in chat | It is not `ready`, not accessible to you, or retrieval is capped for the turn. |
| Voice Live reports that the gateway or realtime service is unavailable | The active provider's APIM WebSocket API, its scoped key, or its upstream backend (Foundry for Azure OpenAI, the Azure AI Services account for Azure Speech) is unavailable. Retry after gateway health is restored; each provider's API and key are independent, so one provider's outage does not necessarily affect the other. |
| Voice Live fails before opening the socket | API public URL, Origin allowlist, browser microphone permission, or auth is misconfigured. |
| Voice Live was connected but stopped hearing me | The browser muted or ended the microphone track, or audio processing could not recover. The app now closes the session and shows a reconnect message; confirm OS/browser input state before retrying. |
| Azure Speech is not offered as a provider | The operator has not enabled it (`AI4IA_SPEECH_VOICE_LIVE_ENABLED` off, or it is not in the server's voice provider allowlist). Azure OpenAI remains available. |
| A provider/model change appears not to apply | Voice settings intentionally affect the next connection. Stop and reconnect; the current socket is never silently replaced. |
| Azure OpenAI Voice Live fails while other paths work | The UI now reports bounded protocol/close details for operator correlation, but that alone does not prove an Azure OpenAI upstream cause. Report the time, provider/model, and safe correlation/error shown; do not paste tokens, audio, transcripts, prompts, or tool data. |
| Admin resource panel is unavailable | The resource id is empty, the API identity lacks Monitoring Reader, or Azure Monitor data is unavailable. |
