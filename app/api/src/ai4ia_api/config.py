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
    gateway_api_version: str = "2024-10-21"
    gateway_timeout_seconds: float = 120.0
    # Override the chat-completions path template (placeholders: {deployment}).
    # When unset, a sensible default is derived from the provider style.
    gateway_chat_path: str | None = None

    # --- Sessions store ---
    session_store: SessionStoreKind = SessionStoreKind.memory
    cosmos_endpoint: str | None = None
    cosmos_database: str = "ai4ia"

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
    def dev_auth_permitted(self) -> bool:
        return self.env == Environment.local or self.allow_dev_auth

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
