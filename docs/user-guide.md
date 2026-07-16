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
4. Pick a model only when the default is not right for the task. The app clamps
   model parameters to catalog limits.

## Chat well

- Put goals and constraints in the message. Mention files, models, regions, or
  customer context when they matter.
- Use attachments for one-off context in the current session.
- Use the document library for reusable material that should be searchable across
  sessions.
- Treat cited document snippets and tool output as grounded context, not as
  permission to skip review.

## Agents and workflows

- Use built-in agents for common jobs.
- Create a user agent when you repeatedly need a specific role, instruction set,
  model, or tool bundle.
- Use workflows for repeatable multi-step work. Workflows run through the same
  governed tool path as chat.
- Attach only the tools an agent needs. Tool output is metered, logged, bounded,
  and redacted where applicable.

## Documents and media

AI4IA has two document paths:

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

## Voice

- Turn-based speech uses the normal API path.
- Voice Live uses a browser WebSocket directly to the API ingress because the
  Next.js proxy does not proxy WebSockets.
- The API still enforces auth, Origin checks, entitlements, metering, deployment
  selection, and optional governed tools.
- The orange live microphone starts and stops Voice Live inside the current chat.
  The normal transcript and composer stay available, and finalized spoken turns
  are saved into that same session.
- Open **Voice settings** beside the composer to choose the current/default or an
  enabled agent, a **provider**, a voice, and optional governed tools.
  **Advanced** includes instructions, temperature, turn detection, transcription,
  and a language hint. Settings are stored in this browser, stale values fall back
  safely, and controls lock while connected because changes apply to the next
  session.
- Two providers are available when an operator enables both: **Azure OpenAI**
  (the default, with a catalog realtime model and its usual voice/turn-detection
  options) and **Azure Speech** (a second, opt-in provider fixed to one managed
  model and a curated set of built-in voices, locale, transcription, noise
  suppression, echo cancellation, and turn-detection choices — there is no custom
  voice, custom endpoint, or free-text voice name option). If only Azure OpenAI is
  configured, the provider control is not shown.
- Changing the provider (or any other voice setting) applies starting with the
  **next** connection, not the current one; it never triggers a silent reconnect
  mid-session. The chat transcript and session are shared across both providers,
  so switching providers keeps the same conversation.
- You can type while Voice Live is connected. Typed turns are saved immediately
  in the shared transcript; because an open realtime socket cannot be reseeded,
  they become Voice Live context the next time it connects.
- Starting Voice Live in an empty chat does not create a chat record until a real
  finalized voice turn needs saving. Denied microphone access or a gateway failure
  leaves the session list unchanged; use the inline **Retry** action after fixing
  connectivity. This holds for both providers.

If Voice Live controls are hidden, the feature is disabled for that environment. If
only the provider selector is hidden, only the default provider is configured.

## Generated artifacts

Image, video, document-processing, and export tools return durable artifacts when
the relevant feature and storage are configured. Artifacts are served through
authenticated API routes rather than public blob URLs.

## Memory

When memory is enabled, AI4IA can recall prior user context and can save ready
document summaries to memory. You can forget saved document memories through the
document controls. Current gap: there is no global memory toggle or recalled-memory
indicator in the chat UI.

## Custom tools and web search

Custom MCP servers and Web IQ search tools are feature-gated. When enabled:

- MCP server credentials are stored in Key Vault outside local development.
- Remote MCP endpoints pass an SSRF guard before discovery and each use.
- Search and browse output is bounded and treated as untrusted model context.

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
App (replicas, restarts), Cosmos DB, Azure AI Search, and PostgreSQL (CPU, storage,
connections). Each tile degrades to unavailable when its Azure resource id, the
`azure-monitor-query` SDK, or the API identity's Monitoring Reader permission is
missing; a `—` cell means no data for that metric, not an error.

## Data boundaries

- User identity is normalized at the API boundary.
- Sessions, messages, usage, agents, workflows, MCP server records, and document
  manifests are canonical in Cosmos DB.
- Derived memory vectors, search indexes, chunks, parsed artifacts, and media
  sidecars can be rebuilt from canonical records and blob storage.
- Model calls route through the configured gateway unless a native Azure service
  control plane is required.

## Known gaps

- The library UI is document-centric; backend support for image, audio, and video
  exists, but uploads are not first-class in the picker.
- Custom analyzer authoring, folder-level sharing, and anonymous public links are
  not implemented.
- Memory has save/forget and automatic recall, but no global user-facing toggle or
  recalled-memory indicator.

## Troubleshooting

| Symptom | What it usually means |
| --- | --- |
| Feature controls are missing | The web feature flag is off or the API feature is disabled. |
| A library route returns disabled/not found | Document understanding is not enabled or prerequisites failed startup validation. |
| A document does not appear in chat | It is not `ready`, not accessible to you, or retrieval is capped for the turn. |
| Voice Live reports that the gateway or realtime service is unavailable | The active provider's APIM WebSocket API, its scoped key, or its upstream backend (Foundry for Azure OpenAI, the AIServices account for Azure Speech) is unavailable. Retry after gateway health is restored; each provider's API and key are independent, so one provider's outage does not necessarily affect the other. |
| Voice Live fails before opening the socket | API public URL, Origin allowlist, browser microphone permission, or auth is misconfigured. |
| Azure Speech is not offered as a provider | The operator has not enabled it (`AI4IA_SPEECH_VOICE_LIVE_ENABLED` off, or it is not in the server's voice provider allowlist). Azure OpenAI remains available. |
| Admin resource panel is unavailable | The resource id is empty, the API identity lacks Monitoring Reader, or Azure Monitor data is unavailable. |
