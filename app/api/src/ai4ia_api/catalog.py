"""Loads the curated model catalog and resolves model -> deployment routing.

Precedence: explicit path (settings/env) -> packaged ``data/model_catalog.json``
-> repo ``infra/models.json`` fallback (dev only, transformed on the fly).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, computed_field

from .model_traits import reasoning_effort_options, supports_sampling

_PACKAGED = Path(__file__).resolve().parent / "data" / "model_catalog.json"

# Categories whose models are driven through a normal text chat turn and so
# belong in the chat / agent model pickers. Everything else — image, video, tts,
# transcription, audio, realtime, embedding, rerank — is a capability model
# invoked through its own surface or tool, not selected as a raw chat target.
# This is an allowlist on purpose: a new capability category added to the
# catalog stays out of the chat picker until it's explicitly opted in here.
CONVERSATIONAL_CATEGORIES = frozenset(
    {"chat", "chat-fast", "reasoning", "reasoning-oss", "router", "research"}
)

# Data-residency policy tokens, weakest constraint first:
#
#   global  no constraint (default; any deployment may serve)
#   zonal   processing must stay inside SOME data zone, caller does not mind
#           which -- rejects GlobalStandard, accepts any DataZone/regional
#           deployment. Maximises model choice while still excluding
#           "may process anywhere on earth".
#   us/eu   processing must stay inside that specific zone.
#
# `zonal` exists because Azure's DataZoneStandard availability is uneven: four
# models offer it in eastus2 but not swedencentral. Forcing a choice between
# "global" and a specific zone would either deny those models to everyone or
# make `us` and `eu` silently unequal with no way to express "regional, either
# one". A caller on `zonal` still gets a real boundary, and the per-option
# `residency` field (and the usage ledger's region/dataZone) says which zone
# actually served -- so a user can apply their own sovereignty judgement rather
# than being told a guarantee that does not fit.
#
# Lowercase so they compare directly against both the settings value and
# ``DeploymentOption.residency``.
GLOBAL_RESIDENCY = "global"
ZONAL_RESIDENCY = "zonal"
RESIDENCY_POLICIES = frozenset({GLOBAL_RESIDENCY, ZONAL_RESIDENCY, "us", "eu"})


class DeploymentOption(BaseModel):
    region: str
    dataZone: str | None = None
    sku: str
    deploymentName: str

    @computed_field
    @property
    def residency(self) -> str:
        """Where this deployment's processing may actually occur.

        This is deliberately NOT ``dataZone``. ``dataZone`` describes the
        *endpoint's geography*; residency describes the *processing scope*, and
        the two diverge for the SKU that serves almost every model here:

        * ``GlobalStandard`` -- Azure may process the request in any region
          worldwide, whatever the endpoint's geography. A GlobalStandard
          deployment in Sweden Central is therefore ``global``, not ``eu``.
          Labelling it ``eu`` (as ``dataZone`` alone does) is the exact claim a
          sovereignty control must not make.
        * ``DataZoneStandard`` -- bounded to the endpoint's data zone.
        * ``Standard`` -- regional; processing stays in that region, which is
          strictly stronger than its data zone, so reporting the data zone is
          correct and conservative.

        Values are lowercase (``global`` / ``us`` / ``eu``) so they compare
        directly against :class:`~ai4ia_api.config.Settings.data_residency`.
        """
        if self.sku == "GlobalStandard":
            return GLOBAL_RESIDENCY
        if not self.dataZone:
            # No recorded zone: fail OPEN on the label, not on the guarantee --
            # "global" is the weakest claim, so an unknown zone can never be
            # mistaken for a residency promise.
            return GLOBAL_RESIDENCY
        return self.dataZone.strip().lower()

    def satisfies(self, policy: str) -> bool:
        """Whether this deployment is usable under a residency ``policy``.

        ``global`` accepts everything. ``zonal`` accepts any deployment whose
        processing is bounded to a data zone, without caring which. A specific
        zone accepts only deployments bounded to that zone -- a ``global``
        deployment does NOT satisfy ``zonal``, ``us`` or ``eu``, because "may
        process anywhere" cannot satisfy "must stay inside a boundary".
        """
        if policy == GLOBAL_RESIDENCY:
            return True
        if policy == ZONAL_RESIDENCY:
            return self.residency != GLOBAL_RESIDENCY
        return self.residency == policy


class ModelEntry(BaseModel):
    id: str
    displayName: str
    category: str
    format: str
    # Which provider surface serves this model: "chat" (Chat Completions, the
    # default), "responses" (required by gpt-5-pro/gpt-5-codex/o3-pro),
    # "anthropic" (Claude Messages), "mai" (MAI's OpenAI-compatible chat
    # surface on /mai/v1), or "bfl" (Black Forest Labs image generation).
    # The gateway routes by this flag; the field is informational to the UI.
    api: str = "chat"
    # Per-model context window (total prompt+completion tokens the deployment
    # accepts) and the maximum tokens it will emit in one completion. Both are
    # OPTIONAL: when absent (``None``) the backend falls back to its fixed
    # constants, so a model lacking metadata behaves exactly as before. Populated
    # for conversational models from ``infra/models.json``; serialized so the web
    # app can show the cap and clamp the max-tokens input from one source of truth.
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    # Optional provider-specific image controls. Absent means the shared image
    # defaults; a recorded list is authoritative for validation and tooling.
    imageSizes: list[str] | None = None
    imageQualities: list[str] | None = None
    # ``reasoning_effort`` values this model accepts, from ``infra/models.json``.
    # ``None`` means "not recorded" and falls back to the family heuristic;
    # an empty list means "recorded, and this model takes no effort value".
    # The distinction matters: a reasoning model with no recorded data should
    # still get the safe low/medium/high floor rather than losing the control.
    reasoningEffort: list[str] | None = None
    options: list[DeploymentOption]

    @computed_field
    @property
    def conversational(self) -> bool:
        """Whether this model is offered in the chat/agent model pickers.

        True for text-chat categories (chat, reasoning, router, …); False for
        capability models (image, video, tts, transcription, embedding, rerank)
        and voice models (realtime, audio), which are reached through their own
        surfaces/tools rather than selected as a raw chat target. Serialized so
        the web app can filter the dropdowns from the same source of truth.
        """
        return self.category in CONVERSATIONAL_CATEGORIES


    @computed_field
    @property
    def supportsSampling(self) -> bool:
        """Whether ``temperature``/``top_p`` actually reach the provider.

        Reasoning models 400 on non-default sampling values, so the gateway
        strips them from the outgoing body. Serializing that here lets the web
        app hide the sliders instead of presenting controls that are silently
        discarded -- which is what it did for 11 of the 15 conversational models,
        including the whole GPT-5.6 family.
        """
        return supports_sampling(self.id)

    @computed_field
    @property
    def reasoningEffortOptions(self) -> list[str]:
        """Allowed ``reasoning_effort`` values, empty when unsupported.

        The one knob reasoning models *do* honour, and the UI offered no way to
        set it. The values must come from the server: they vary per model in ways
        no naming convention predicts (``gpt-5.6`` rejects ``minimal`` that
        ``gpt-5.4`` accepts; ``gpt-5-pro`` accepts only ``high``), so a hardcoded
        list in the web app would offer values that 400.

        Precedence is catalog first, heuristic second. ``infra/models.json``
        carries per-model values established by probing the live deployment;
        ``model_traits`` supplies only the conservative floor every reasoning
        model honours, so a newly added model degrades to fewer options rather
        than to options that fail.
        """
        if self.reasoningEffort is not None:
            return list(self.reasoningEffort)
        return reasoning_effort_options(self.id)


class ModelCatalog(BaseModel):
    models: list[ModelEntry]
    # App-wide data-residency policy, set once at startup from
    # ``Settings.data_residency``. It lives on the catalog rather than being
    # passed per call because EVERY consumer reaches routing through the single
    # ``app.state.catalog`` instance -- so the policy cannot be forgotten at a
    # call site, which is what "server-authoritative" has to mean for a
    # sovereignty control.
    residencyPolicy: str = GLOBAL_RESIDENCY

    def get(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

    def eligible_options(self, entry: ModelEntry) -> list[DeploymentOption]:
        """This model's deployments that are usable under the active policy."""
        return [o for o in entry.options if o.satisfies(self.residencyPolicy)]

    def available(self, entry: ModelEntry) -> bool:
        """Whether the policy leaves this model reachable at all."""
        return bool(self.eligible_options(entry))

    def conversational_models(self) -> list[ModelEntry]:
        """Models the chat/agent pickers should offer.

        Excludes capability models, and -- under a restrictive residency policy
        -- any model with no compliant deployment. Offering a model the policy
        forbids would produce a confusing failure at send time instead of an
        honest absence at selection time.
        """
        return [m for m in self.models if m.conversational and self.available(m)]

    def resolve_deployment(
        self, model_id: str, *, region: str | None = None, data_zone: str | None = None
    ) -> DeploymentOption | None:
        """Pick a deployment for a model, honoring an explicit region/data zone.

        An explicit constraint is treated as a REQUIREMENT, not a hint. This
        previously fell through to ``entry.options[0]`` when the requested region
        or data zone had no deployment, so a caller asking for EU processing
        silently got a US one and nothing in the response, the usage record or
        the logs said the request had been relocated. For a residency constraint
        that failure mode is worse than an error, so an unsatisfiable constraint
        now returns ``None``; every caller already maps that to a 400-class
        "unknown or unavailable model" response.

        Constraints combine with AND when both are supplied: asking for a region
        *and* a data zone that disagree is contradictory, and answering from
        whichever one happened to match first is exactly the silent relocation
        this avoids.

        The app-wide ``residencyPolicy`` is applied FIRST and cannot be widened
        by a caller: a request may narrow routing further, never escape the
        deployment's configured sovereignty envelope.

        NOTE: a caller-supplied ``data_zone`` filters on the endpoint's
        geography (``DeploymentOption.dataZone``), which is a weaker statement
        than ``residency``. Use the policy for an actual guarantee.
        """
        entry = self.get(model_id)
        if entry is None or not entry.options:
            return None
        options = self.eligible_options(entry)
        if region:
            options = [o for o in options if o.region == region]
        if data_zone:
            options = [o for o in options if o.dataZone == data_zone]
        return options[0] if options else None


def _transform_infra_models(raw: dict[str, Any]) -> dict[str, Any]:
    naming = raw["naming"]
    sku_short = naming["skuShort"]
    regions = raw.get("regions", {})
    # Single source of truth in infra/models.json `naming` (matches main.bicep + the catalog
    # generator). This dev fallback only runs from a source checkout without the packaged catalog.
    token = naming["subscriptionToken"]
    models = []
    for model in raw["catalog"]:
        options = []
        for dep in model["deployments"]:
            region = dep["region"]
            sku = dep["sku"]
            options.append(
                {
                    "region": region,
                    "dataZone": regions.get(region, {}).get("dataZone"),
                    "sku": sku,
                    "deploymentName": f"{model['name']}-{token}-{region}-{sku_short[sku]}",
                }
            )
        models.append(
            {
                "id": model["name"],
                "displayName": model.get("displayName", model["name"]),
                "category": model.get("category", "chat"),
                "format": model["format"],
                "api": model.get("api", "chat"),
                "contextWindow": model.get("contextWindow"),
                "maxOutputTokens": model.get("maxOutputTokens"),
                "reasoningEffort": model.get("reasoningEffort"),
                "options": options,
            }
        )
    return {"models": models}


def _load_raw(explicit_path: str | None) -> dict[str, Any]:
    if explicit_path:
        return json.loads(Path(explicit_path).read_text(encoding="utf-8"))
    if _PACKAGED.exists():
        return json.loads(_PACKAGED.read_text(encoding="utf-8"))
    # Dev fallback: transform infra/models.json if running from a source checkout.
    infra = Path(__file__).resolve().parents[4] / "infra" / "models.json"
    if infra.exists():
        return _transform_infra_models(json.loads(infra.read_text(encoding="utf-8")))
    raise FileNotFoundError("No model catalog found (packaged or infra).")


@lru_cache
def load_catalog(
    explicit_path: str | None = None,
    residency: str = GLOBAL_RESIDENCY,
    claude_enabled: bool = True,
) -> ModelCatalog:
    """Load the catalog under a data-residency and provider entitlement policy.

    An unrecognised policy fails closed to the most permissive value rather than
    silently filtering everything out; ``Settings`` validates the value first, so
    reaching that branch means a caller bypassed configuration.

    ``claude_enabled`` defaults true for catalog tooling/tests that inspect the
    complete source of truth. The running API always passes its default-off
    setting explicitly, so an Anthropic model cannot appear in chat or agent
    pickers before its Marketplace fulfillment is ready.
    """
    raw = _load_raw(explicit_path)
    policy = residency if residency in RESIDENCY_POLICIES else GLOBAL_RESIDENCY
    models = [
        model
        for model in raw["models"]
        if claude_enabled or model.get("api") != "anthropic"
    ]
    return ModelCatalog(models=models, residencyPolicy=policy)
