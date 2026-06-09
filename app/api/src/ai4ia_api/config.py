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


@lru_cache
def get_settings() -> Settings:
    return Settings()
