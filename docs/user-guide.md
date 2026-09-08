# AI4IA User Guide

AI4IA keeps chat, agents, documents, memory, tools, and voice in one workspace.
The most useful distinction is between **what you ask for**, **what context the
model receives**, and **what actions it is allowed to take**. The Conversation
Inspector makes those choices visible; the API enforces them.

## Start

1. Open your environment's web app and sign in with Microsoft Entra ID.
   Local development may instead use a configured development identity.
2. Start a conversation or reopen one from the sidebar.
3. Open the Conversation Inspector: **Setup** controls the model, instructions,
   agent, tools, and voice; **Context** controls documents and memory; **Usage**
   explains the recorded consumption.
4. Describe the outcome you need and attach or select only the relevant sources.

Features vary by deployment. A hidden control can mean the operator disabled the
capability or the selected model cannot use it; it is not a permission you can
grant by changing a browser setting.

## Chat well

Give the assistant a goal, constraints, and an expected output. Use attachments
for one-off material and the library for sources you expect to reuse.
Check citations against the original source before relying on an answer.

Model controls reflect the server's catalog. Context size, output limits,
reasoning effort, sampling, input modalities, and tool support vary by model.
**Plain chat only** models can answer ordinary questions but cannot run agents
or workflows that need tools. A larger context window is a capacity limit, not
a promise that every document will be included or every fact recalled.

A selected agent is the standing persona. A leading `@agent` mention overrides
it for one turn, for example `@coder explain this function`; a mention later in
the message is ordinary text. Type `@` at the start for available agents, or
use `/agents`. The internal `@conversation` badge means conversation-attached
tools without a selected agent, not another agent to invoke.

The inspector shows inherited instructions and tools alongside conversation
overrides. Saved server values, not an unsaved control or a model's claim about
its abilities, determine the next turn.

## Agents and workflows

| Use | Best fit | Important boundary |
| --- | --- | --- |
| Plain conversation | A question or exploratory task | Context and model limits still apply |
| Agent | A reusable persona, model, and tool bundle | Attach only the tools it needs |
| Workflow | An ordered, repeatable set of steps | Each step has its own effective capabilities |
| Durable workflow | Work that should survive an API restart | Requires deployment support and an explicit per-run choice |

In the workflow editor, **Build** defines the steps; **Run & test** runs them and
keeps their results visible. The result distinguishes completed, failed, and
unstarted steps. **Open in chat** opens the run's conversation.

**Tools for this step** adds capabilities to the selected agent's tools. Read
the effective capability list: chat-only capabilities are not promised in a
workflow. In particular, a step cannot upload or analyze a new library document;
document-review templates work on sources already uploaded and ready.

Memory tools are explicit per step. Enable **Save memory** when a step must
store a fact, and look for the tool's result rather than trusting text such as
"I've remembered that." A deduplicated fact can correctly report that nothing
new was stored. Under **Documents**, a non-empty workflow selection restricts
the run; selecting none allows the run to read your ready library documents.

**Keep running if the app restarts** uses Azure Durable Task Scheduler.
The page may stop waiting after two minutes without cancelling the run; its
result can still arrive in the run's chat. Without this option, a replica
restart can interrupt the request. Durability does not grant additional tools,
remove approval requirements, or undo a tool's external effects.

The chat **Run workflow** tool is deliberately narrower than the workflow
editor. It advertises only enabled workflows whose resolved steps are safe,
read-only, non-recursive, and compatible with that execution path.

### Agent activity

Activity shows observable work: searching, reading, invoking tools, being
blocked, or failing. A completed turn retains that bounded activity history.

An **Execution receipt** gives more detail: the effective redacted prompt,
model/region, admitted and displaced context, source versions, offered and
invoked tools, bounded arguments/results, approvals, usage, and safety coverage.
Loaded skills include their source URI, version resolution, hash, and truncation.
Long workflows also retain independently bounded step receipts.

**These are not chain-of-thought.** They show what the application supplied and
executed, not the model's private reasoning or proof of which source caused an
answer. Shortened payloads retain their original redacted size and digest.

## Documents and media

The single **Attach** control accepts the types and limits advertised by the
server. Uploads run sequentially with visible progress and retry/dismiss actions.
A library upload becomes selected conversation context only after association
succeeds. Navigation is temporarily blocked while an upload is active so it
cannot land in a different conversation.

| Path | Use it for | Lifetime and scope |
| --- | --- | --- |
| Session attachment | One-off material for this chat | Bounded, session-scoped context; not a reusable library entry |
| Document library | Reusable documents, images, audio, or video | Owner-scoped source bytes, analysis, manifest, and retrieval index |

Only **ready** library documents participate in retrieval, sharing, media
deep-links, memory saves, and document tools. Upload acceptance alone does not
mean analysis and indexing have completed.

### Choose an analyzer

- **Automatic - Content Understanding** chooses the modality-appropriate Azure
  analyzer and is the normal default.
- **Mistral Document AI / Mistral OCR 4** are explicit PDF/image alternatives,
  limited to 30 pages and 30 MB per request.
- When enabled, **Content Understanding Read / Layout** are preview,
  synchronous options for small files: 10 MB and the first five PDF pages.

The analyzer is part of deduplication: the same bytes analyzed two ways produce
separately attributable results. Ready Content Understanding documents can expose
**Evidence** with structured fields, confidence, grounding, and provider details.
Confidence needs workload-specific interpretation; it is not a correctness
guarantee. See [document and multimodal understanding](document-multimodal-understanding.md).

### Where Azure AI Search fits

You do not upload separately to Search. Library ingestion extracts content,
chunks it, obtains embeddings, and indexes the chunks. Retrieval combines
keyword and vector search, with semantic reranking when configured.

Deployments can use per-user or shared indexes; access filtering still applies
to every query. An explicit conversation selection is an allowlist. Clearing it
to an empty selection disables library context; older sessions without a
selection retain the all-accessible behavior. Revoked sharing is rechecked, so
a stale selected id cannot restore access.

Search is derived state. Cosmos owns the manifest and Blob owns the source
bytes and parsed artifacts. Without a Search endpoint, the library uses an
in-memory chunk store even outside local development. That index is replica-local
and lost on restart; stored summaries and parsed-document reads remain available.
It is not equivalent to shared, persistent retrieval on a scaled deployment.

### Managing the index

Owner-scoped maintenance endpoints under `/api/library` separate retrieval from
the original document:

| Action | Endpoint | Additional model work |
| --- | --- | --- |
| Inspect one | `GET /documents/{id}/index` | None |
| Rebuild one | `POST /documents/{id}/reindex` | Embeddings |
| Rebuild all your ready documents | `POST /documents/reindex` | Embeddings |
| Remove one from retrieval | `DELETE /documents/{id}/chunks` | None |

Reindexing reuses saved extraction, not another analyzer run. The saved
`chunks.jsonl` sidecar preserves boundaries and media grounding; older documents
without it fall back to parsed Markdown and may lose time grounding.
Removing chunks leaves the document and analysis intact. These are maintenance
operations, not agent tools; rebuilding is metered and entitlement-gated.

### Sharing

**Private** means owner-only, **shared** grants read access by email, and
**public** means readable by authenticated users of the configured tenant.
Public does **not** create an anonymous internet link.

Sharing revocation affects subsequent reads. It does not erase snippets already
saved in another conversation or its historical receipt.

## Voice

The orange microphone starts and stops Voice Live in the current conversation.
Finalized spoken turns are saved to the normal transcript. **Play** on an
assistant message is separate text-to-speech and does not require a live socket.

Open **Setup > Voice** for provider, model, voice, locale, and supported audio
options. Azure OpenAI uses catalogued realtime deployments. Optional Azure
Speech uses a curated managed-model catalog in East US 2; it does not accept
arbitrary model names, custom endpoints, or personal voices.

Settings apply to the **next connection** without silently reconnecting the
current one. The API supplies the selected agent persona or saved conversation
instructions; voice has no competing instructions field.

You can type while connected. Typed turns save immediately but enter the live
provider's context on its next connection. A failed microphone permission or
connection attempt does not create an empty conversation. If saving finalized
turns fails, use **Retry** or **Discard**; stopping still releases the microphone
and socket. A lost/muted microphone or unrecoverable audio context closes the
connection rather than leaving a misleading "live" indicator.

Voice connects directly to the API's WebSocket ingress, through its separately
scoped APIM route. It does not bypass authentication, Origin validation,
entitlements, metering, or tool governance.

## Generated artifacts

Image, video, processing, and export tools create durable artifacts when enabled.
Downloads use authenticated API routes, not anonymous Blob links.

For images, open **Setup > Agent & tools > Image generation** in a saved
conversation. Select one to three models and a size/quality they share, then
save. **Start image in chat** or **Start comparison in chat** adds
`/generate_image` without discarding your draft. Each request snapshots the setup;
comparison output stays in selection order and records its model and deployment
provenance.

Video generation is asynchronous and slower than a text reply. Supported clip
lengths are 4, 8, or 12 seconds, with 4 seconds as the default.

**Cost estimate unavailable** is not free. Published estimates can differ from
Azure billing, especially for provider-specific media meters.

## Memory

Memory can carry personal context between conversations. In **Context > Memory**,
**Automatic memory** is on by default. Turn it off to stop automatic recall,
saving, and model memory tools across chats, agents, and workflows, including
delayed/resumed work. This is a capability switch, not a consent ceremony or
tool approval. It does not enable a backend disabled by the operator.

You can still create, list, edit, or delete your own records while it is off.
User-created or edited memories are protected from automatic consolidation.
The control keeps a change pending even if you switch conversations or close and
reopen the inspector. After the request settles it reloads your current server
setting. If confirmation fails, the last confirmed setting is restored visually
but marked unconfirmed; reload it before trying again. Changing accounts never
applies one owner's pending result to another. Turning memory back on makes
retained records available to new work.

`/forget` removes this conversation's memories; `/forget me` removes all active
memories for your profile. Deleting a document also fences and removes memory
derived from it. A stale edit produces a conflict rather than overwriting a
newer version.

Expand **Memories supplied** below an answer for the bounded memory context
recorded in its execution receipt, links to your inspector items, and an entry
point to the full receipt. It describes supplied context, not which memory
influenced a sentence or hidden reasoning. Memory tool returns are shown
separately because a return alone does not prove later model delivery. Old
answers without provenance stay **unrecorded**, and truncated or missing
references are not reconstructed from your current records.

Disabling or deleting memory does not rewrite old answers or historical receipts.
Already-sent prompts cannot be withdrawn, and existing transcript text can still
be sent as conversation history. Provider backups retain their own retention
window.
See [memory architecture](memory.md) for the deletion boundary.

## Custom tools and web search

Custom MCP servers and WebIQ require deployment support. MCP connection secrets
live in Key Vault outside local development. Remote endpoints are checked before
discovery and again when invoked. Neither a remote server nor a retrieved page
can grant itself permission.

Ask for live information in chat, or use `/research <query>`. WebIQ is a tool
provider, not an `@webiq` agent. The available model tools are:

| Tool | Purpose |
| --- | --- |
| `web_search` | Web results and source content |
| `news_search` | News, publisher information, and timestamps |
| `video_search` | Videos, playlists, summaries, and timestamped moments |
| `image_search` | Existing images and source-page metadata |
| `browse_url` | Public HTTPS page content and returned links |
| `classic_search` | Structured answers such as weather, finance, places, and events |
| `finance_search` | Instrument prices and available as-of metadata |
| `places_search` | Places, businesses, hours, and available contact data |
| `sports_search` | Schedules, scores, and event data |
| `sonic_search` | Blended web/news/finance search |
| `web_autosuggest` | Query suggestions; beta entitlement, not an answer source |

These are tools, not eleven slash commands. Filters vary by endpoint. Classic
search supports 30 categories but returns at most six answer types per call.
An omitted type is not evidence that no information exists.

Safe search stays strict; output is bounded, redacted, and treated as untrusted.
Returned links are not followed automatically. A crawl may report `pending`
instead of content; there is no implied background polling. Credentials do not
prove entitlement to every vertical or beta endpoint.

### Approving a call

Under the default policy, browsing and sandbox computation require approval
because the model chooses a destination or program. Other outbound first-party
tools, such as WebIQ search, prompt when the turn also carries untrusted
document, memory, or tool context.

The card identifies the tool, destination, and bounded argument preview.
Warnings identify hidden or omitted arguments. Approve only if the action
matches your intent: a source can contain instructions designed to steer the
assistant. Each approval is bound to one call's exact arguments, expires after
ten minutes, and cannot authorize a different call or conversation.

### Auto-approving enabled tools

When available, **Setup > Agent & tools** lets you opt in for the current saved
conversation. **Run & test** has a separate per-run workflow choice that resets
for the next invocation. Consent is not active until the server confirms it.

Consent covers only the recorded enabled-tool contracts, lasts at most eight
hours, and can be revoked. New tools or changed contracts need renewed consent.
Session consent does not authorize a workflow invocation. A workflow step that
requires approval but has no run consent fails visibly rather than running with
unattended authority.

**The tradeoff is real:** hostile source content can influence later calls
while per-call prompts are skipped. Ownership, scopes, destination checks, and
usage limits still apply, and activity/receipts retain approval provenance.
Revocation stops subsequent dispatch; it cannot undo an external request already
in flight. **Revoke auto-approval & stop run** preserves completed/partial
workflow evidence.

## Usage and admin views

Usage reports known token, image, page, and estimated-cost subtotals with coverage.
Missing billing dimensions remain **Unknown**, not zero. Prompt pressure
describes the latest token-metered turn and may be unavailable after switching
models or when the provider omits prompt usage.

Application quotas are **soft preflight checks, not hard spending caps**.
Concurrent requests can overshoot, missing provider prices/usage can undercount,
and ledger-check failures can allow work. Azure budget notifications are also
alerts rather than a mechanism that stops spending.

Admin access is enforced by the API. Admins can inspect usage by model, user,
agent, date, deployment, and request outcome, plus Azure resource metrics and
fixed-query operations panels. Capped scans are labelled truncated; unavailable,
partial, and stale sources remain distinguishable. Telemetry does not provide
complete proxy queue/fairness or provider-quota forecasting.

## Data boundaries

Cosmos is canonical for conversations, usage, agents/workflows, document
manifests, and **memory text and vectors**. Blob holds source documents and
generated artifacts. Search indexes and document chunks are rebuildable;
deleting canonical data is a different operation.

Conversation deletion is not transactional erasure across all stores: an
already-authorized concurrent write can leave an orphaned child record. A
durable cleanup/reconciliation design is still needed; do not treat a successful
delete response as a physical-erasure guarantee.

A model's region or data-zone selection concerns inference routing, not where
your conversation and documents are stored. Global deployments are not
region-resident just because their account has a regional name. Read the
[region and capability map](region-capability-matrix.md) before using sensitive
material with a residency requirement.

Provider safety assessments are observations, not proof of safety. AI4IA's
recorded policy is non-blocking assessment visibility; provider-native refusals
still apply and modality coverage remains incomplete.

## Troubleshooting

| Symptom | First thing to check |
| --- | --- |
| A control is missing | Deployment availability and the selected model's capabilities |
| A document is absent from context | Ready state, access, explicit selection, and the turn's context budget |
| A memory edit conflicts | Reload the latest record before retrying |
| Voice fails before connecting | Microphone permission, sign-in, API URL, and allowed Origin |
| Voice settings seem unchanged | Stop and reconnect; settings affect the next connection |
| Speech is not offered | The operator's provider allowlist and Speech feature gate |
| Search or another tool fails | Its visible error/approval state; an enabled gate does not prove upstream entitlement |
| An admin panel is unavailable | Resource wiring, API identity permissions, and source freshness |

Report the time, selected model/provider, and safe correlation/error code.
Do not paste access tokens, prompts, audio, documents, or tool payloads into
general operational logs.
