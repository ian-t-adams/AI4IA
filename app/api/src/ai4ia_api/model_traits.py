"""Per-model request-shape traits, shared by the gateway and the catalog.

These live in their own module because two callers need the same answer and a
second definition would drift silently: ``gateway.client`` uses them to rewrite
an outgoing request body, and ``catalog.ModelEntry`` serializes them so the web
app can render only the controls a model actually honours. If the UI carried its
own copy of the rule it would keep offering sliders the gateway strips, which is
exactly the bug these traits exist to prevent.
"""
from __future__ import annotations

import re

# Azure OpenAI reasoning models (the GPT-5 family and the o-series) reject the
# classic Chat Completions sampling/limit parameters: they require
# ``max_completion_tokens`` instead of ``max_tokens`` and 400 on non-default
# ``temperature``/``top_p``/penalties/logprobs. A deployment name always begins
# with the catalog model id (e.g. ``gpt-5.2-slurmfactory-eastus2-glbl``), so a
# leading-id match is a reliable signal for both a model id and a deployment
# name. ``model-router`` is deliberately EXCLUDED: it accepts the standard
# parameter set and drops the unsupported ones itself when it routes to an
# o-series model (per Microsoft Learn), so we must not pre-transform it.
REASONING_DEPLOYMENT = re.compile(r"^(gpt-5|o1|o3|o4)\b", re.IGNORECASE)

# The reasoning_effort values EVERY reasoning model accepts. This is a floor, not
# a description of any particular model: the real per-model set is recorded in
# ``infra/models.json`` (``reasoningEffort``) from probing the live deployments,
# because it varies in ways no naming convention predicts. Probed 2026-07-31
# against sub-planetexpress-slurmfactory: the whole gpt-5.6 family REJECTS
# "minimal" while gpt-5.4 accepts it, "xhigh" is accepted by models it is not
# documented for, and gpt-5-pro accepts "high" and nothing else. Microsoft Learn
# disagrees with all three, so the docs are not a safe source here.
# Offering a value the model rejects is a 400 the user sees; offering one value
# too few just falls back to the model's own default. Hence the floor.
_UNIVERSAL_REASONING_EFFORTS = ["low", "medium", "high"]
# o1-mini is the one reasoning model that accepts no reasoning_effort at all.
_NO_REASONING_EFFORT = re.compile(r"^o1-mini\b", re.IGNORECASE)


def is_reasoning_deployment(name: str) -> bool:
    """True for an Azure OpenAI reasoning model id or deployment name."""
    return bool(REASONING_DEPLOYMENT.match(name))


def supports_sampling(name: str) -> bool:
    """True when the model honours ``temperature``/``top_p``.

    Reasoning models 400 on non-default values, so the gateway strips them; the
    UI must not present them as if they did something.
    """
    return not is_reasoning_deployment(name)


def reasoning_effort_options(name: str) -> list[str]:
    """Conservative fallback ``reasoning_effort`` values, ``[]`` when unsupported.

    Only consulted for a model with no ``reasoningEffort`` in the catalog. See
    ``_UNIVERSAL_REASONING_EFFORTS`` for why this deliberately under-reports.
    """
    if not is_reasoning_deployment(name) or _NO_REASONING_EFFORT.match(name):
        return []
    return list(_UNIVERSAL_REASONING_EFFORTS)
