"""Typed application settings sourced from environment variables.

Fail-closed posture (per security review):
- The dev auth provider is only allowed when ``env == local`` or
  ``allow_dev_auth`` is explicitly true; otherwise startup fails.
- In ``prod`` the model gateway must require inbound auth (never ``none``).
- ``entra`` auth requires a tenant + audience; ``cosmos`` store requires an
  endpoint.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    local = "local"
    dev = "dev"
    prod = "prod"


class AuthProviderKind(str, Enum):
    dev = "dev"
    entra = "entra"


class GatewayProviderStyle(str, Enum):
    # Azure/Foundry-native: deployment in the path + api-version query param.
    azure_openai_native = "azure_openai_native"
    # OpenAI-compatible: `model` in the body, no deployment path segment.
    openai_compatible = "openai_compatible"


class GatewayAuthMode(str, Enum):
    none = "none"
    api_key = "api_key"
    bearer = "bearer"


class SessionStoreKind(str, Enum):
    memory = "memory"
    cosmos = "cosmos"


class MemoryStoreKind(str, Enum):
    # Per-user semantic memory is off entirely.
    disabled = "disabled"
    # In-process cosine store: good for local/dev/tests; not durable across
    # restarts or replicas.
    in_memory = "in_memory"
    # Our custom embed+store backend on Postgres + pgvector (gateway embeddings,
    # exact cosine scan). Durable, AAD-auth, no LLM extraction.
    pgvector = "pgvector"
    # The real mem0 OSS library (LLM fact-extraction + consolidation) over the
    # same Postgres + pgvector, calling our model gateway for its LLM/embeddings.
    # Selecting it requires postgres_host + postgres_user (see validate_runtime).
    mem0 = "mem0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AI4IA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Environment = Environment.local

    # --- Auth ---
    auth_provider: AuthProviderKind = AuthProviderKind.dev
    allow_dev_auth: bool = False
    dev_user_sub: str = "dev-user"
    dev_user_name: str = "Dev User"
    dev_user_email: str = "dev@example.com"

    entra_tenant_id: str | None = None
    entra_audience: str | None = None
    # Comma-separated list of tenant IDs allowed when multi-tenant.
    entra_allowed_tenants: str | None = None

    # --- Model gateway ---
    model_gateway_url: str = "http://localhost:9099"
    model_gateway_auth_mode: GatewayAuthMode = GatewayAuthMode.none
    model_gateway_api_key: str | None = None
    gateway_provider_style: GatewayProviderStyle = GatewayProviderStyle.azure_openai_native
    gateway_api_version: str = "2025-04-01-preview"
    gateway_timeout_seconds: float = 120.0
    # Image generation (Phase 7A) has its own api-version + timeout: image models
    # (gpt-image-2 etc.) can take much longer than a chat turn and may track a
    # different supported api-version than chat. 2024-10-21 (GA) is verified
    # against the deployed gpt-image-2 deployment.
    gateway_image_api_version: str = "2024-10-21"
    gateway_image_timeout_seconds: float = 180.0
    # Video generation (Phase 11G, Sora 2) is an async job: create -> poll ->
    # download. The Sora REST surface tracks the ``preview`` api-version. Each
    # individual HTTP call uses ``gateway_video_timeout_seconds``; the service
    # polls every ``gateway_video_poll_interval_seconds`` up to a hard
    # ``gateway_video_max_wait_seconds`` ceiling so a turn can never hang.
    gateway_video_api_version: str = "preview"
    gateway_video_timeout_seconds: float = 60.0
    gateway_video_poll_interval_seconds: float = 5.0
    gateway_video_max_wait_seconds: float = 240.0
    # Voice (Phase 7B) — speech-to-text (whisper) and text-to-speech
    # (gpt-4o-mini-tts / tts-hd) ride the same gateway/auth path as chat. They
    # track their own api-version and a generous timeout (audio synthesis /
    # transcription can exceed a chat turn). gpt-4o-mini-tts speech is only
    # served on a 2025 api-version (2024-10-21 GA returns 404 for it), and that
    # api-version also serves whisper transcription, so both share one version.
    gateway_audio_api_version: str = "2025-03-01-preview"
    gateway_audio_timeout_seconds: float = 120.0
    # --- Voice Live (Phase 10): real-time speech-to-speech relay ---
    # Feature-flagged and default-OFF: with realtime_enabled=False the
    # ``/api/voice/live`` WebSocket refuses (closes immediately), so the app's
    # default behavior is byte-for-byte unchanged. When enabled, the browser
    # connects to the API's external ingress (the Next.js HTTP proxy can't proxy
    # WebSockets) and the relay opens the upstream realtime socket through the
    # same model gateway as chat — the browser never sees the deployment or the
    # gateway credential. Governance (auth + entitlement gate + metering +
    # Origin/auth validation) is enforced in the relay.
    realtime_enabled: bool = False
    # Azure OpenAI realtime preview api-version. The preview surface is reached at
    # ``{base}/realtime?api-version=<v>&deployment=<dep>`` (the GA surface uses
    # ``/v1/realtime?model=`` instead); 2025-04-01-preview is verified against the
    # deployed gpt-realtime (version 2025-08-28) on Microsoft Learn.
    realtime_api_version: str = "2025-04-01-preview"
    # Upstream base for the realtime WebSocket. Defaults to the model gateway URL
    # (which already carries the ``/openai`` suffix), so realtime rides the same
    # governed path as chat. http(s) is converted to ws(s) at connect time.
    realtime_base_url: str | None = None
    # Upstream connect/handshake timeout (seconds).
    realtime_timeout_seconds: float = 30.0
    # Optional hard clamp on a single live session's total duration (seconds);
    # 0 disables the clamp. Defense against an abandoned/runaway socket holding a
    # metered session open indefinitely.
    realtime_max_session_seconds: float = 0.0
    # Comma-separated browser Origin allowlist for the relay handshake. WS
    # handshakes are not CORS-preflighted, so the relay validates Origin itself.
    # Empty reflects (allows any) ONLY in the local env; in any deployed env an
    # empty allowlist rejects every Origin (fail-closed).
    realtime_allowed_origins: str | None = None
    # Server-side governed tool calling inside a live session. Default OFF and, like
    # every realtime knob, inert unless ``realtime_enabled`` is also true — so the
    # app's default behavior stays byte-for-byte unchanged. When BOTH are on, the
    # relay injects the safe built-in tools (calculator, get_current_time) into the
    # client's ``session.update`` and executes the model's function calls in-process
    # through the same governed registry/executor as chat (authorize -> validate ->
    # run), so the browser never gains a new capability surface. Disabling this
    # leaves the relay a transparent pump (the merged Phase 10 behavior).
    realtime_tools_enabled: bool = False
    # Override the chat-completions path template (placeholders: {deployment}).
    # When unset, a sensible default is derived from the provider style.
    gateway_chat_path: str | None = None
    # Request token usage in streamed responses via stream_options.include_usage.
    # Defensive: if a deployment rejects it (HTTP 400), the client retries the
    # stream once without it, so this can stay on safely.
    gateway_stream_include_usage: bool = True

    # --- Usage metering (Phase 6): observational cost/traffic ledger ---
    # When true, each completed chat turn is metered and written to a per-user
    # ledger. Best-effort: a ledger write failure never breaks a chat response.
    usage_metering_enabled: bool = True

    # --- Entitlement enforcement (Phase 6B) ---
    # The mechanism is present but ships effectively UNLIMITED: with no per-user
    # override and no global default_* cap below, every user is unlimited. An
    # admin sets a per-user limit to govern a specific user. Set false to bypass
    # the check entirely.
    entitlements_enabled: bool = True
    # TTL for the in-process effective-entitlement cache (keeps the unlimited hot
    # path off Cosmos; an admin change propagates within this window).
    entitlement_cache_ttl_seconds: int = 30
    # Admin gate for the entitlement-management API. Identity-based admins
    # (subjects/emails/roles) are only trusted under non-spoofable auth (entra,
    # or local dev). Under dev auth in a DEPLOYED env the X-Dev-User header is
    # spoofable, so admin then additionally requires a matching X-Admin-Secret;
    # with no secret configured, admin is fail-closed there.
    admin_subjects: str | None = None
    admin_emails: str | None = None
    admin_api_secret: str | None = None
    # Optional GLOBAL default limits applied to users without an override. All
    # unset == fully unlimited (the shipped posture). micro-USD to match the ledger.
    default_requests_per_minute: int | None = None
    default_tokens_per_day: int | None = None
    default_cost_per_day_micro_usd: int | None = None
    default_tokens_per_month: int | None = None
    default_cost_per_month_micro_usd: int | None = None

    # --- Sessions store ---
    session_store: SessionStoreKind = SessionStoreKind.memory
    cosmos_endpoint: str | None = None
    cosmos_database: str = "ai4ia"

    # --- Document & multimodal understanding (Phase 11A): storage spine ---
    # Feature-flagged and default-OFF. With document_understanding_enabled=False
    # the per-user document library is never constructed and the ``/api/library``
    # API refuses (404), so the app's default behavior is unchanged. 11A ships
    # only the storage spine (per-user manifest + analyzers registry + ownership/
    # dedupe helpers); Content Understanding ingest, chunking, and retrieval land
    # in later sub-phases. The existing per-session Phase 7C upload path
    # (``/api/sessions/{id}/documents``) is independent and always available.
    document_understanding_enabled: bool = False
    # Max bytes accepted for a single library upload (bound resource use). Reused
    # by the 11B ingest path; declared here so the cap is one source of truth.
    document_max_upload_bytes: int = 50_000_000
    # Max documents retained per user in the library (0 = unlimited).
    document_max_per_user: int = 200

    # --- Custom tools / bring-your-own MCP servers (Phase 12A) ---
    # Feature-flagged and default-OFF. With custom_tools_enabled=False the
    # per-user MCP-server registry is never constructed and the
    # ``/api/agents/mcp-servers`` API refuses (404), so the app's default
    # behavior is byte-for-byte unchanged. When enabled, a user can register
    # remote MCP (Streamable HTTP) servers; we connect behind a strict SSRF
    # egress guard, cache the tools they advertise, and project each onto the
    # existing tool-governance seam (external risk, host-scoped egress, approval
    # required unless the server is marked trusted). Per-turn execution + durable
    # Key-Vault secrets land in Phase 12B. Caps are generous defaults the owner
    # can tighten via the override below.
    custom_tools_enabled: bool = False
    # Per-user MCP-server cap (0 = use the module default). The owner wanted the
    # ability to cap, not tight limits, so this defaults to the generous module
    # constant unless overridden.
    custom_tools_max_servers_per_user: int = 0
    # Discovery connect/handshake timeout (seconds) for the MCP client.
    custom_tools_discovery_timeout_seconds: float = 15.0
    # Azure Key Vault URI backing durable MCP connection secrets (Phase 12B). When
    # set, authenticated servers' credentials are stored here (only an opaque
    # reference is kept on the Cosmos record) and resolved at connect/execute
    # time; when empty the service falls back to a process-local store (local/dev
    # only — see ``validate_runtime``).
    custom_tools_secret_vault_uri: str | None = None

    # --- Azure AI Search (indexing/retrieval) ---
    # Endpoint of the provisioned search service, e.g.
    # ``https://<svc>.search.windows.net``. Reached via the api managed identity
    # (RBAC: Search Index Data Contributor + Service Contributor) — no admin keys.
    # Empty/None when no search service is provisioned (the feature is dormant).
    search_endpoint: str | None = None
    # Name of the document-chunk index. Created lazily (idempotently) on first use
    # when ``search_endpoint`` is set. A single shared index, scoped per-user by a
    # filterable ``user_id`` field, mirroring the pgvector ``doc_chunks`` table.
    search_index_name: str = "ai4ia-doc-chunks"
    # When a query carries text (RAG retrieval always does), the Azure AI Search
    # backend issues a *hybrid* query (vector + BM25 keyword) and, when this flag is
    # on, applies the L2 *semantic* reranker for materially better top-k ordering.
    # If the semantic tier is unavailable (quota/SKU), the store degrades gracefully
    # to plain hybrid — never breaking retrieval. Only affects the AI Search backend;
    # pgvector / in-memory ignore it. Default ON (the search.bicep enables free
    # semantic ranking); set ``AI4IA_SEARCH_SEMANTIC_RANKING=false`` to force hybrid.
    search_semantic_ranking: bool = True

    # --- Content Understanding ingest (Phase 11B) ---
    # CU is its own async REST surface (PUT analyzer / POST :analyzeBinary / GET
    # poll), NOT an OpenAI deployment. Reached at
    # ``{cu_base_url}/contentunderstanding/...``. Default empty/OFF; only used when
    # document_understanding_enabled AND a base url is configured (enforced by
    # validate_runtime outside local). api-version is verified GA on Microsoft
    # Learn (supported: 2024-12-01-preview, 2025-05-01-preview, 2025-11-01).
    cu_base_url: str | None = None
    cu_api_version: str = "2025-11-01"
    # bearer == AAD managed identity token (Cognitive Services scope) when no
    # static key is set, mirroring the gateway's bearer mode; api_key sends the
    # CU resource key in the ``Ocp-Apim-Subscription-Key`` header.
    cu_auth_mode: GatewayAuthMode = GatewayAuthMode.bearer
    cu_api_key: str | None = None
    cu_timeout_seconds: float = 30.0
    # Poll the analyze operation this often, up to this ceiling, before giving up
    # (the manifest then degrades to ``failed`` with the quick-text fallback kept).
    cu_poll_interval_seconds: float = 2.0
    cu_max_poll_seconds: float = 300.0
    # Concrete prebuilt CU analyzer ids per modality. The "*Search" prebuilts are
    # RAG-optimized (Markdown + grounded fields). Overridable per deployment.
    cu_document_analyzer: str = "prebuilt-documentSearch"
    cu_image_analyzer: str = "prebuilt-imageSearch"
    cu_audio_analyzer: str = "prebuilt-audioSearch"
    cu_video_analyzer: str = "prebuilt-videoSearch"

    # --- Document blob storage (Phase 11B) ---
    # Blob account endpoint, e.g. ``https://<acct>.blob.core.windows.net``. Raw
    # uploads + parsed artifacts live here under ``{userId}/{documentId}/...`` and
    # are reached ONLY via the api managed identity (AAD) — the browser never gets
    # a blob URL. Required (with cu_base_url) when the feature is enabled outside
    # local; in local/dev an in-memory blob store is used when this is unset.
    document_blob_account_url: str | None = None
    document_blob_container: str = "documents"
    # Number of retrieval chunks to embed/index per document is bounded by the
    # chunker; these shape the markdown chunker (chars per chunk + overlap).
    document_chunk_chars: int = 1200
    document_chunk_overlap: int = 150
    # Safety ceiling on indexed chunks per document (0 = unlimited): bounds a
    # pathological multi-MB parse from embedding/inserting an unbounded number of
    # vectors in a single enrich. document_embed_batch caps how many chunks are
    # embedded + inserted per round-trip.
    document_max_chunks: int = 5000
    document_embed_batch: int = 128

    # --- Generated-image blob storage (Phase 11F) ---
    # Durable home for images produced by the ``generate_image`` agent tool. Each
    # artifact lives under ``{userId}/generated/{id}.png`` and is reached ONLY via
    # the api managed identity (AAD) — the browser fetches it through an
    # authenticated serve endpoint, never a public blob URL. Unset locally/in
    # tests, where a process-local in-memory store is used instead. Independent of
    # the document library: image generation ships live, so its storage is not
    # gated on the document-understanding flag.
    image_blob_account_url: str | None = None
    image_blob_container: str = "images"

    # --- Generated-video blob storage (Phase 11G) ---
    # Durable home for MP4 clips produced by the ``generate_video`` agent tool.
    # Each artifact lives under ``{userId}/generated/{id}.mp4`` and is reached
    # ONLY via the api managed identity (AAD) through an authenticated serve
    # endpoint, never a public blob URL. Unset locally/in tests, where a
    # process-local in-memory store is used instead. May reuse the image storage
    # account (a distinct container) or a dedicated one.
    video_blob_account_url: str | None = None
    video_blob_container: str = "videos"

    # --- Retrieval consumer (Phase 11B-2): how the ready library surfaces in chat.
    # Tier 1 (always-injected summary cards) + Tier 2 (top-k RAG chunks) are bounded
    # so the library context can never crowd out the conversation. Tier 3 is the
    # fetch_document tool, whose single read is capped by document_fetch_max_chars.
    # All retrieval is gated by document_understanding_enabled and only ever
    # surfaces `ready` documents (a failed/analyzing doc can never contribute).
    document_retrieval_top_k: int = 6
    document_context_max_docs: int = 20
    document_context_max_chars: int = 8000
    document_fetch_max_chars: int = 12000

    # --- Compute over the library (Phase 11C): intent router + code_interpreter
    # + "adjust & return" export. Layered ON TOP of document_understanding: a
    # second default-OFF flag so the highest-regression-risk surface (the chat hot
    # path) is inert unless explicitly enabled. When off, the intent router never
    # runs, neither synthetic tool is advertised, and chat is byte-for-byte
    # unchanged. Requires document_understanding_enabled (enforced in
    # validate_runtime), since compute reads the same ready library.
    document_compute_enabled: bool = False
    # Code Interpreter rides the Azure OpenAI **Responses API** built-in
    # ``code_interpreter`` tool (a sandboxed Azure-managed Python container), NOT
    # an APIM/chat-completions deployment. Verified on Microsoft Learn: ``POST
    # {code_interpreter_base_url}/openai/v1/responses`` with
    # ``tools:[{type:"code_interpreter",container:{type:"auto"}}]``. The v1 GA
    # surface omits ``api-version``; set ``preview`` to opt into latest preview
    # features. Base url is the bare resource endpoint, e.g.
    # ``https://<resource>.openai.azure.com`` (``/openai/v1`` is appended by the
    # client). Required (with the model) when compute is enabled outside local.
    code_interpreter_base_url: str | None = None
    # Deployment/model name that serves the Responses API CI tool (e.g. gpt-4.1).
    code_interpreter_model: str | None = None
    code_interpreter_api_version: str = ""
    # bearer == AAD managed-identity token (Azure OpenAI v1 scope) when no static
    # key is set; api_key sends the resource key in the ``api-key`` header.
    code_interpreter_auth_mode: GatewayAuthMode = GatewayAuthMode.bearer
    code_interpreter_api_key: str | None = None
    # AAD scope for a managed-identity token against the v1 Responses endpoint.
    # Verified on Microsoft Learn (Entra samples for /openai/v1): ai.azure.com.
    code_interpreter_aad_scope: str = "https://ai.azure.com/.default"
    code_interpreter_timeout_seconds: float = 120.0
    # Max chars of parsed document text handed to CI as fenced input per run, and
    # max chars of a single "adjust & return" exported artifact. Both bound how
    # much untrusted content moves through the compute path.
    code_interpreter_max_input_chars: int = 60000
    document_export_max_chars: int = 200000
    # Raw-file compute: when ON, ``run_code`` uploads the document's ORIGINAL bytes
    # (the uploaded PDF/xlsx/csv/…) to the Responses Files API and attaches them to
    # the code-interpreter container (``container.file_ids``), so the sandbox reads
    # the real file rather than its CU-parsed text. Only the Azure-OpenAI
    # CI-supported file types are eligible; anything else (or an oversize/missing
    # original, or any upload failure) transparently falls back to the parsed-text
    # path — so this never breaks an existing run. Default OFF (parsed-text only).
    code_interpreter_raw_files_enabled: bool = False
    # Hard cap on the original-file size handed to the interpreter (bytes). A larger
    # original falls back to the parsed-text path rather than uploading. 25 MiB.
    code_interpreter_max_raw_file_bytes: int = 26_214_400

    # --- Inline-attachment code interpreter (default-OFF). Lets the chat agent
    # crack/analyze an INLINE composer attachment (routers/documents.py, Phase 7C)
    # with the SAME Code Interpreter sandbox the library ``run_code`` tool uses,
    # reading the REAL uploaded file (PDF layout / xlsx cells / image) rather than
    # the cheap local text extract. Augments — does NOT replace — the instant
    # text-extract path: the local extract still feeds chat context every turn; this
    # only adds an agent-callable ``analyze_attachment`` tool for heavy/binary files
    # or compute-needing tasks. Default OFF: when off, NO original bytes are
    # retained, NO ephemeral store is touched, and the tool is never advertised, so
    # the inline-document path is byte-for-byte unchanged. Reuses the existing
    # code_interpreter_* settings + the raw-file size cap above (no second CI config
    # surface). Requires code_interpreter_base_url + code_interpreter_model when
    # enabled outside local (enforced in validate_runtime), since every analysis is
    # a Responses-API CI call.
    inline_document_compute_enabled: bool = False
    # Dedicated blob container for the EPHEMERAL retained originals. Reuses the
    # document blob account/managed-identity wiring (document_blob_account_url); a
    # separate container keeps the short-lived inline bytes clearly apart from the
    # durable library corpus and lets infra attach a lifecycle/TTL expiry rule to it
    # without touching library data. Unset locally/in tests -> an in-memory store.
    inline_attachment_blob_container: str = "ephemeral-attachments"

    # --- Document processing tool (Phase 11H): the agent-callable
    # ``process_document`` tool. Rides the same document_understanding flag and
    # ready library as compute/retrieval (no new flag, no new infra): the tool is
    # only injected when the retrieval consumer is constructed. These bound how
    # much parsed text is fed to the single analysis call, how large a result may
    # be, and the inline-vs-artifact split (results at or under the inline cap are
    # returned inline; larger ones persist to a per-user artifact and return a
    # reference). The inline cap stays well under the runtime's ~8 KB tool-result
    # limit so a returned-inline result can never overflow the tool channel.
    document_processing_max_input_chars: int = 60000
    document_processing_max_output_chars: int = 100000
    document_processing_inline_max_chars: int = 6000

    # --- Web IQ search (default-OFF). Exposes Microsoft Web IQ (web / news /
    # videos / images / browse) as synthetic tools to ANY tool-enabled agent + the
    # main chat, via the official ``webiq`` SDK (lazy-imported; see
    # ai4ia_api.websearch). Default OFF: when off the factory returns None, NO SDK
    # client is constructed, and NO web tool is ever advertised, so the chat hot
    # path is byte-for-byte unchanged. When enabled outside local, an
    # ``webiq_api_key`` OR ``webiq_use_entra`` (EntraID DefaultAzureCredential) is
    # required (enforced fail-closed in validate_runtime), since every search is a
    # billed Web IQ network call. ``webiq_base_url`` overrides the API endpoint.
    # The two caps bound the synthetic tools' fan-out (results per search) and the
    # browse-content length returned to the model.
    web_search_enabled: bool = False
    webiq_api_key: str | None = None
    webiq_base_url: str | None = None
    webiq_use_entra: bool = False
    web_search_max_results: int = 5
    web_search_max_content_chars: int = 6000

    # --- Rolling summarization (Phase WS2-C): sustainable long conversations ---
    # DEFAULT-OFF. When off, the chat path sends today's full history byte-for-byte
    # and never injects a summary block — the manual ``/summarize`` command still
    # works and persists a running summary, but it does not alter what subsequent
    # turns send while this flag is off. When ON, once the assembled transcript
    # would exceed the model-derived threshold, the oldest turns are folded into
    # the session's running summary (kept incrementally) and only the newest
    # ``summarization_recent_turns`` turns are sent verbatim alongside the summary;
    # the FULL transcript is always retained in storage + the UI scrollback.
    auto_summarization_enabled: bool = False
    # How many of the most-recent turns (user/assistant messages) to always keep
    # verbatim, never folded into the summary.
    summarization_recent_turns: int = 6
    # Fraction of the model's context window (token budget, ~4 chars/token) the
    # live transcript may occupy before older turns are folded. The remainder is
    # headroom for the system prompt, memory/doc/library blocks, the summary
    # itself, and the reserved max-output.
    summarization_threshold_ratio: float = 0.5
    # Threshold (in characters) used when the active model declares no context
    # window, so the auto path still has a bound to trigger on.
    summarization_fallback_threshold_chars: int = 48_000
    # Cap on the tokens spent generating the summary itself (keeps the digest
    # compact and the fold cheap).
    summarization_max_output_tokens: int = 1024

    # --- Memory (Phase 5): per-user semantic recall ---
    # Single source of truth: the store kind both selects the backend AND gates
    # the feature (``disabled`` == off). No separate enable flag, so the two can
    # never disagree.
    memory_store: MemoryStoreKind = MemoryStoreKind.disabled
    # Catalog model id used to embed memories + queries (resolved to a deployment
    # through the same model gateway). Its native dimension must match
    # ``memory_embedding_dimensions`` (encoded now so the pgvector schema in the
    # next increment is not a breaking migration).
    memory_embedding_model: str = "text-embedding-3-large"
    memory_embedding_dimensions: int = 3072
    # Retrieval shaping.
    memory_top_k: int = 5
    memory_min_score: float = 0.25
    # Injection caps (defense against memory-poisoning / context bloat): how many
    # recalled snippets to inject, max chars per snippet, and max total chars.
    memory_max_injected: int = 5
    memory_max_chars_per_item: int = 500
    memory_max_total_chars: int = 2000
    # Don't store trivially short user utterances ("ok", "thanks").
    memory_min_chars_to_store: int = 12
    # Save-to-memory (Phase 11E-1): an explicit "remember this document" action
    # promotes a library document's gist into durable memory. Bound how many
    # memory records one save creates (the summary plus leading excerpts) and how
    # large each excerpt is, so a big document can neither flood recall nor blow
    # the injection budget. ``max_items`` includes the summary card.
    memory_document_max_items: int = 6
    memory_document_chunk_chars: int = 600
    # --- Real mem0 backend (memory_store=mem0) ---
    # mem0 runs an LLM "fact-extraction" pass on each remembered utterance, then
    # consolidates. This model must be NON-reasoning (mem0 sends temperature +
    # max_tokens + response_format=json_object, which GPT-5/o-series reject).
    memory_extraction_model: str = "gpt-4.1-mini"
    # pgvector table mem0 owns. Distinct from the custom store's "memories" table
    # so the two backends coexist in the same DB and the flip stays reversible.
    mem0_collection_name: str = "mem0_memories"
    # Short-term message-history cache (SQLite). Ephemeral + per-replica in
    # Container Apps (the durable memories live in pgvector), so a writable
    # scratch path is fine and avoids any home-dir permission surprise.
    mem0_history_db_path: str = "/tmp/mem0_history.db"  # noqa: S108 - intended scratch
    # remember() runs an inline LLM extraction call; bound it so a slow gateway
    # can't stall the (best-effort) chat path. recall/forget get a tighter bound.
    mem0_add_timeout_s: float = 20.0
    mem0_op_timeout_s: float = 12.0
    # Minimum hybrid-relevance score for a recalled memory. mem0's scoring blends
    # semantic + BM25 + entity boosts and is NOT the cosine scale memory_min_score
    # uses, so it gets its own knob; mem0's own default is 0.1.
    mem0_search_threshold: float = 0.1
    # Serialize concurrent mem0 calls a little: its sync providers + the ephemeral
    # SQLite history run in worker threads, where unbounded concurrency can cause
    # "database is locked". Cheap insurance; raise if throughput ever needs it.
    mem0_max_concurrency: int = 4
    # Postgres connection (pgvector backend). host/user are required when
    # memory_store=pgvector (enforced in validate_runtime). user is the AAD
    # principal name the api managed identity was registered under as a Postgres
    # role (no SQL passwords — auth is an AAD token, see PgVectorStore).
    postgres_host: str | None = None
    postgres_database: str = "mem0"
    postgres_user: str | None = None
    postgres_port: int = 5432

    # --- Observability ---
    log_level: str = "INFO"
    applicationinsights_connection_string: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AI4IA_APPLICATIONINSIGHTS_CONNECTION_STRING",
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
        ),
    )

    # --- Admin resource metrics (WS4 Part B): Azure Monitor platform metrics ---
    # The admin dashboard's resource panels (AI Search query volume/latency,
    # Postgres CPU/storage/connections, Cosmos RU, Container Apps replicas/restarts)
    # are read from Azure Monitor via the api managed identity (RBAC: Monitoring
    # Reader on each resource). Each value below is a full ARM resource id, e.g.
    # ``/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Search/searchServices/<name>``.
    # ALL optional and default OFF: a panel whose id is unset (or whose query fails,
    # or where the azure-monitor-query SDK is absent) reports ``unavailable`` rather
    # than erroring, so the dashboard ships before WS3 wires the diagnostics/ids.
    resource_metrics_enabled: bool = True
    metrics_search_resource_id: str | None = None
    metrics_postgres_resource_id: str | None = None
    metrics_cosmos_resource_id: str | None = None
    metrics_container_app_resource_id: str | None = None

    # Optional path override for the bundled model catalog (tests/dev).
    model_catalog_path: str | None = None
    # Optional path override for the bundled agent catalog (tests/dev).
    agent_catalog_path: str | None = None

    @property
    def allowed_tenants(self) -> list[str]:
        raw = self.entra_allowed_tenants or self.entra_tenant_id or ""
        return [t.strip() for t in raw.split(",") if t.strip()]

    @property
    def admin_subject_set(self) -> set[str]:
        raw = self.admin_subjects or ""
        return {s.strip() for s in raw.split(",") if s.strip()}

    @property
    def admin_email_set(self) -> set[str]:
        raw = self.admin_emails or ""
        return {s.strip().lower() for s in raw.split(",") if s.strip()}

    @property
    def dev_auth_permitted(self) -> bool:
        return self.env == Environment.local or self.allow_dev_auth

    @property
    def realtime_effective_base_url(self) -> str:
        """Upstream base for the realtime WS (falls back to the model gateway)."""
        return self.realtime_base_url or self.model_gateway_url

    @property
    def realtime_allowed_origin_list(self) -> list[str]:
        raw = self.realtime_allowed_origins or ""
        return [o.strip() for o in raw.split(",") if o.strip()]

    def cu_analyzer_for_modality(self, modality: str) -> str:
        """Concrete prebuilt CU analyzer id for a modality (the ingest default)."""
        return {
            "document": self.cu_document_analyzer,
            "text": self.cu_document_analyzer,
            "image": self.cu_image_analyzer,
            "audio": self.cu_audio_analyzer,
            "video": self.cu_video_analyzer,
        }.get(modality, self.cu_document_analyzer)

    def validate_runtime(self) -> None:
        """Enforce fail-closed invariants. Call at startup."""
        if self.auth_provider == AuthProviderKind.dev and not self.dev_auth_permitted:
            raise RuntimeError(
                "Dev auth is disabled outside local. Set AI4IA_AUTH_PROVIDER=entra "
                "or AI4IA_ALLOW_DEV_AUTH=true (local/test only)."
            )
        if self.auth_provider == AuthProviderKind.entra:
            if not self.entra_tenant_id or not self.entra_audience:
                raise RuntimeError(
                    "Entra auth requires AI4IA_ENTRA_TENANT_ID and AI4IA_ENTRA_AUDIENCE."
                )
        if (
            self.env == Environment.prod
            and self.model_gateway_auth_mode == GatewayAuthMode.none
        ):
            raise RuntimeError(
                "Model gateway auth must not be 'none' in prod. Configure "
                "AI4IA_MODEL_GATEWAY_AUTH_MODE=api_key|bearer."
            )
        if (
            self.model_gateway_auth_mode == GatewayAuthMode.api_key
            and not self.model_gateway_api_key
        ):
            raise RuntimeError("AI4IA_MODEL_GATEWAY_API_KEY is required for api_key auth mode.")
        if self.session_store == SessionStoreKind.cosmos and not self.cosmos_endpoint:
            raise RuntimeError("AI4IA_COSMOS_ENDPOINT is required for the cosmos session store.")
        if self.memory_store in (MemoryStoreKind.pgvector, MemoryStoreKind.mem0) and (
            not self.postgres_host
        ):
            raise RuntimeError(
                f"AI4IA_POSTGRES_HOST is required for the {self.memory_store.value} memory store."
            )
        if self.memory_store in (MemoryStoreKind.pgvector, MemoryStoreKind.mem0) and (
            not self.postgres_user
        ):
            raise RuntimeError(
                f"AI4IA_POSTGRES_USER is required for the {self.memory_store.value} memory store."
            )
        if self.entitlements_enabled and not self.usage_metering_enabled:
            # Budgets/rate limits accrue from the usage ledger; with metering off
            # every positive limit silently never trips (only disabled and
            # 0-hard-blocks would work). Refuse the half-broken combo so an
            # admin-set limit can never quietly fail to enforce.
            raise RuntimeError(
                "Entitlement enforcement requires usage metering. Set "
                "AI4IA_USAGE_METERING_ENABLED=true, or disable enforcement with "
                "AI4IA_ENTITLEMENTS_ENABLED=false."
            )
        if self.realtime_enabled and self.env != Environment.local and (
            not self.realtime_allowed_origin_list
        ):
            # An empty Origin allowlist reflects (allows any) only in local. In a
            # deployed env that means the relay would reject every browser, so a
            # realtime-enabled deploy with no allowlist is a misconfiguration —
            # fail closed at startup rather than ship a feature that 1008s every
            # client.
            raise RuntimeError(
                "Voice Live requires an Origin allowlist outside local. Set "
                "AI4IA_REALTIME_ALLOWED_ORIGINS to the web origin(s), or disable "
                "the feature with AI4IA_REALTIME_ENABLED=false."
            )
        if (
            self.document_understanding_enabled
            and self.env != Environment.local
            and self.session_store != SessionStoreKind.cosmos
        ):
            # The per-user document library is durable cross-session storage; in a
            # deployed env the in-memory store would silently lose every manifest
            # on restart/scale. Require the Cosmos store so the feature is durable,
            # or keep it disabled. Local/dev may use the in-memory store freely.
            raise RuntimeError(
                "Document understanding requires the cosmos session store outside "
                "local (set AI4IA_SESSION_STORE=cosmos), or disable it with "
                "AI4IA_DOCUMENT_UNDERSTANDING_ENABLED=false."
            )
        if (
            self.document_understanding_enabled
            and self.env != Environment.local
            and (not self.cu_base_url or not self.document_blob_account_url)
        ):
            # The 11B ingest path needs both the Content Understanding endpoint
            # (the parse front door) and the blob account (raw + parsed artifacts).
            # A deployed enable without them would accept uploads it can't store or
            # crack, so fail closed rather than ship a half-wired feature. Local/dev
            # may run with an in-memory blob store and CU disabled.
            raise RuntimeError(
                "Document understanding requires AI4IA_CU_BASE_URL and "
                "AI4IA_DOCUMENT_BLOB_ACCOUNT_URL outside local, or disable it with "
                "AI4IA_DOCUMENT_UNDERSTANDING_ENABLED=false."
            )
        if (
            self.document_understanding_enabled
            and self.cu_base_url
            and self.cu_auth_mode == GatewayAuthMode.api_key
            and not self.cu_api_key
        ):
            # api_key mode with no key would silently send no auth header and only
            # fail at the first CU call (every upload degrades to ``failed``). Fail
            # loud at startup instead. Runs in any env once CU is configured.
            raise RuntimeError(
                "AI4IA_CU_API_KEY is required when AI4IA_CU_AUTH_MODE=api_key. "
                "Set the key, switch to bearer (managed identity), or disable the "
                "feature with AI4IA_DOCUMENT_UNDERSTANDING_ENABLED=false."
            )
        if self.document_compute_enabled and not self.document_understanding_enabled:
            # Compute reads the same per-user *ready* library (CI input, export
            # source) the retrieval consumer surfaces. Enabling it without the
            # storage/ingest spine would advertise a router + tools over a library
            # that is never constructed — fail closed rather than ship a feature
            # wired to nothing.
            raise RuntimeError(
                "Document compute requires document understanding. Set "
                "AI4IA_DOCUMENT_UNDERSTANDING_ENABLED=true, or disable compute with "
                "AI4IA_DOCUMENT_COMPUTE_ENABLED=false."
            )
        if (
            self.document_compute_enabled
            and self.env != Environment.local
            and (not self.code_interpreter_base_url or not self.code_interpreter_model)
        ):
            # The Code Interpreter path needs the Responses API endpoint + a model
            # deployment to run code against. A deployed enable without them would
            # advertise a run_code tool whose every call fails — fail closed.
            # Local/dev may enable compute with an injected/fake CI client.
            raise RuntimeError(
                "Document compute requires AI4IA_CODE_INTERPRETER_BASE_URL and "
                "AI4IA_CODE_INTERPRETER_MODEL outside local, or disable it with "
                "AI4IA_DOCUMENT_COMPUTE_ENABLED=false."
            )
        if (
            self.document_compute_enabled
            and self.code_interpreter_base_url
            and self.code_interpreter_auth_mode == GatewayAuthMode.api_key
            and not self.code_interpreter_api_key
        ):
            # api_key mode with no key would send no auth header and 401 on the
            # first CI call (every run_code degrades to an error). Fail loud at
            # startup instead, mirroring the CU api_key check.
            raise RuntimeError(
                "AI4IA_CODE_INTERPRETER_API_KEY is required when "
                "AI4IA_CODE_INTERPRETER_AUTH_MODE=api_key. Set the key, switch to "
                "bearer (managed identity), or disable compute with "
                "AI4IA_DOCUMENT_COMPUTE_ENABLED=false."
            )
        if (
            self.inline_document_compute_enabled
            and self.env != Environment.local
            and (not self.code_interpreter_base_url or not self.code_interpreter_model)
        ):
            # The inline-attachment analyzer rides the SAME Responses API Code
            # Interpreter endpoint + model deployment as library compute. A deployed
            # enable without them would advertise an analyze_attachment tool whose
            # every call fails — fail closed. Local/dev may enable it with an
            # injected/fake CI client. (Unlike library compute this does NOT require
            # document_understanding: the inline path is independent of the library.)
            raise RuntimeError(
                "Inline document compute requires AI4IA_CODE_INTERPRETER_BASE_URL "
                "and AI4IA_CODE_INTERPRETER_MODEL outside local, or disable it with "
                "AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED=false."
            )
        if (
            self.web_search_enabled
            and self.env != Environment.local
            and not (self.webiq_api_key or self.webiq_use_entra)
        ):
            # Every Web IQ search is a billed network call that needs auth: an API
            # key OR an EntraID DefaultAzureCredential. A deployed enable without
            # either would advertise web tools whose every call 401s — fail closed.
            # Local/dev may enable with an injected/fake client and no real auth.
            raise RuntimeError(
                "Web search requires AI4IA_WEBIQ_API_KEY or "
                "AI4IA_WEBIQ_USE_ENTRA=true outside local, or disable it with "
                "AI4IA_WEB_SEARCH_ENABLED=false."
            )
        if (
            self.custom_tools_enabled
            and self.env != Environment.local
            and self.session_store != SessionStoreKind.cosmos
        ):
            # The per-user MCP-server registry is durable cross-session storage; in
            # a deployed env the in-memory store would silently lose every
            # registered server on restart/scale. Require the Cosmos store so the
            # registry is durable, or keep the feature disabled. Local/dev may use
            # the in-memory store freely.
            raise RuntimeError(
                "Custom tools (BYO MCP) require the cosmos session store outside "
                "local (set AI4IA_SESSION_STORE=cosmos), or disable it with "
                "AI4IA_CUSTOM_TOOLS_ENABLED=false."
            )
        if (
            self.custom_tools_enabled
            and self.env != Environment.local
            and not self.custom_tools_secret_vault_uri
        ):
            # Authenticated MCP servers persist their connection secret to Key
            # Vault; the process-local fallback store would lose every credential
            # on restart/scale, breaking per-turn execution of authed servers.
            # Require a vault outside local, or keep the feature disabled.
            raise RuntimeError(
                "Custom tools (BYO MCP) require a Key Vault for durable secrets "
                "outside local (set AI4IA_CUSTOM_TOOLS_SECRET_VAULT_URI), or "
                "disable it with AI4IA_CUSTOM_TOOLS_ENABLED=false."
            )
        if (
            self.custom_tools_enabled
            and self.auth_provider == AuthProviderKind.dev
            and self.env != Environment.local
        ):
            # Per-user custom tools (BYO MCP) scope the registry and connection
            # secrets to the signed-in tenant user. Spoofable dev auth (even with
            # allow_dev_auth) would let any caller assume any identity in a
            # deployed env, breaking that isolation. Require real Entra auth, or
            # keep the feature disabled.
            raise RuntimeError(
                "Custom tools require real authentication in a deployed "
                "environment: set AI4IA_AUTH_PROVIDER=entra, or disable them with "
                "AI4IA_CUSTOM_TOOLS_ENABLED=false."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
