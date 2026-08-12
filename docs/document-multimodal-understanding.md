# Document & Multimodal Understanding

AI4IA has two document paths:

1. **Per-session attachments** (`/api/sessions/{id}/documents`) extract capped
   local text and inject it into that session as untrusted context.
2. **The cross-session library** (`/api/library`) stores user-owned documents,
   enriches them with Content Understanding, indexes chunks for retrieval, and
   exposes governed tools over ready documents.

The library is feature-gated by `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED` /
`documentUnderstandingEnabled`. When disabled, the API refuses library routes and
the web app hides the library.

The chat composer exposes one Attach action. When the library is enabled, image,
audio, and video files use the same authenticated Content Understanding ingest path
as reusable documents and show stored/analyzing/ready/failed state. A selected ready
library document is stored on the conversation and scopes retrieval; removing it
from the conversation does not delete the durable library artifact.

Selection is distinct from readiness: a fresh upload is associated while it is
stored/analyzing, remains visible if processing fails, and automatically becomes
effective context when it reaches `ready`. Missing/null selection on a legacy
session means all accessible ready documents; explicit `[]` means none; a non-empty
list is the exact allowlist used by summary/RAG, `fetch_document`, `run_code`,
`export_document`, and `process_document`.

## Pipeline

```mermaid
flowchart LR
  U[Upload] --> API[/POST /api/library/documents/]
  API -->|sha256 + analyzer| DEDUPE{cached?}
  DEDUPE -->|yes| COS[(Cosmos manifest)]
  DEDUPE -->|no| BLOB[(Blob original)]
  API --> QUICK[Local quick text summary]
  BLOB --> CU[Content Understanding analyze]
  CU --> PARSED[parsed.md + grounded fields]
  PARSED --> CHUNKS[chunk + embed]
  PARSED --> MEDIA[media timeline for audio/video]
  CHUNKS --> VECTOR[(Azure AI Search)]
  COS --> CHAT[summary cards + RAG context]
  VECTOR --> CHAT
  BLOB --> TOOLS[fetch_document / run_code / export_document / process_document]
```

Upload returns quickly with a manifest and quick-text fallback. Enrichment runs
asynchronously. Only `ready` documents contribute to chat, retrieval, tools, media
playback, memory save, or sharing.

## Storage model

- **Cosmos `userDocuments`**: canonical manifest, partitioned by `/userId`.
  Includes filename, modality, size, content hash, status, analyzer id, summary,
  sources, chunk count, versions, annotations, `visibility`, and email ACL.
- **Cosmos `analyzers`**: built-in analyzer descriptors and user analyzer records.
  Built-ins are not persisted as user records and cannot be shadowed.
- **Blob Storage**: raw bytes, `parsed.md`, `chunks.jsonl`, media timeline
  sidecars, and versioned/exported artifacts under a user/document prefix.
- **Azure AI Search**: per-user document chunks. Azure AI Search, when
  configured, provides hybrid vector + BM25 retrieval with optional semantic
  reranking.
- **Memory store**: explicit save-to-memory promotes a ready document's summary
  and bounded excerpts into the user's memory backend; forget/delete cascades
  remove those derived memories.

## Retrieval and tools

- **Tier 1:** summary cards for ready accessible documents.
- **Tier 2:** top-k RAG chunks, nonce-fenced as untrusted context.
- **Tier 3:** `fetch_document` for full/partial parsed Markdown.
- **Compute:** `run_code` and `export_document` use Azure OpenAI Responses API
  Code Interpreter when `AI4IA_DOCUMENT_COMPUTE_ENABLED=true`. The sandbox
  receives the document's **parsed text** unless
  `AI4IA_CODE_INTERPRETER_RAW_FILES_ENABLED=true`, which uploads the **original
  bytes** instead so the model reads real spreadsheet cells and PDF layout.
  Unsupported types, oversize originals, and upload failures all fall back to
  parsed text. The setting ships default-`false`, but **this deployment enables
  it** (`infra/main.parameters.json`), so treat original file bytes — not just
  extracted text — as reaching the sandbox when assessing data handling.
- **Inline compute:** `analyze_attachment` can hand the original bytes of an
  inline composer attachment to the same Responses API Code Interpreter endpoint
  when `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED=true`; it is independent of the
  durable library flag and uses short-lived attachment storage.
- **Processing:** `process_document` produces bounded inline output or a durable
  markdown artifact.
- **Media:** audio/video timelines and span-id citations (`[[cite:S1]]`) open the
  web media player at the cited timestamp. The id resolves to the span's own
  `documentId` and start offset, so two ready documents sharing a filename no
  longer collide; rows written before that change still carry the older
  `[[cite:FILENAME@MM:SS]]` token and still resolve by name.

All tool paths re-check ownership/access, require `ready` status, apply caps, and
sanitize untrusted strings before returning them to the model.

## Evaluated alternate parser: Mistral Document AI / OCR

The live subscription offers `mistral-document-ai-2512` (GA) and
`mistral-ocr-4-0` (Preview), but neither is deployed or wired into ingestion.
They are image-to-text models, not conversational models: Microsoft documents
image/PDF input, at most 30 pages / 30 MB, and text/JSON/Markdown output.
Microsoft Learn's Foundry capability table currently lists `en`, while Mistral's
provider material claims broader multilingual coverage (including 170 languages
for OCR 4). Treat the deployed language contract as unverified until a live
fixture proves it; do not advertise either limit from provider marketing alone.

They can become a useful **explicit parser choice** for PDFs and images,
but must not become a second implicit canonical parser. Before enablement:

1. normalize their output into the existing `parsed.md` / manifest / chunk contract;
2. persist parser provider, model/version, region/SKU, and source-page provenance;
3. make analyzer selection explicit and immutable for one ingest attempt;
4. preserve the same ownership, dedupe, retry, deletion, and rebuild semantics;
5. meter by pages/requests rather than pretending OCR is token-priced; and
6. state the narrower format, language, and page limits in the UI.

Content Understanding remains the default because it already handles documents,
images, audio, and video through one durable pipeline. Mistral OCR is a future
specialized path, not a drop-in replacement and not a fallback that silently
changes parsing semantics.

## Sharing and privacy

Documents default to `private`. Owners can set:

- `shared` with an email ACL.
- `public`, which means tenant-authenticated users can open the document; it is
  not an anonymous public link.

Shared documents are searchable/openable by grantees through the same access gate.
Annotations and saved memories remain owner-private and never travel with a share.

## Configuration

Core flags and knobs (these are the **runtime settings** the container receives,
not repo variables — see
[configuration reference](configuration-reference.md#feature-flags-and-prerequisites)
for which of them a repo variable can actually change):

- `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED`
- `AI4IA_CU_BASE_URL`, `AI4IA_CU_API_VERSION`, `AI4IA_CU_AUTH_MODE`
- `AI4IA_DOCUMENT_BLOB_ACCOUNT_URL`, `AI4IA_DOCUMENT_BLOB_CONTAINER`
- `AI4IA_SEARCH_ENDPOINT`, `AI4IA_SEARCH_INDEX_NAME`
- `AI4IA_DOCUMENT_COMPUTE_ENABLED`
- `AI4IA_CODE_INTERPRETER_BASE_URL`, `AI4IA_CODE_INTERPRETER_MODEL`
- `AI4IA_CODE_INTERPRETER_RAW_FILES_ENABLED`, `AI4IA_CODE_INTERPRETER_MAX_RAW_FILE_BYTES`
- `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED`
- `AI4IA_MEMORY_STORE`, `AI4IA_MEMORY_DOCUMENT_MAX_ITEMS`

Outside local, document understanding requires Cosmos session storage, a blob
account URL, and a CU endpoint. Document compute and inline attachment compute
require the Responses API endpoint and model.

## Current gaps

- Custom analyzer authoring is not exposed in the web UI.
- Folder/collection-level sharing is not implemented.
- Anonymous public links are not implemented; `public` remains tenant-walled.
- Optional direct-download SAS for very large files is not implemented.
