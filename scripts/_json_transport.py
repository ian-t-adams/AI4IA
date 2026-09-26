"""Carry JSON-valued deployment variables through azd parameter substitution.

azd 1.29.0 resolves each string parameter in ``infra/main.parameters.json`` by
marshaling its entry to JSON text and substituting environment values into that
text without escaping them (``loadParameters`` in
``cli/azd/pkg/infra/provisioning/bicep/bicep_provider.go``). A value containing a
double quote therefore breaks the file: deploy run 36259812510 failed with
``error unmarshalling Bicep template parameters: invalid character 'v' after
object key:value pair`` for a valid group policy.

Operators still set the raw JSON variable. The parameters file reads only its
``<NAME>_B64`` transport: padded standard base64 (RFC 4648 section 4) of the
exact UTF-8 bytes. That alphabet needs no JSON, dotenv or shell escaping.
``infra/main.bicep`` decodes each transport with ``base64ToString()``, and an
empty transport stays empty. ``scripts/derive-json-transport.py`` derives the
transports in deploy.yml and in the azd preprovision hook, and
``scripts/validate-feature-prereqs.py`` refuses a transport that does not decode
back to the raw value exactly.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass


@dataclass(frozen=True)
class Transport:
    """One raw JSON variable and the names its transport uses."""

    variable: str
    parameter: str
    secret: bool = False
    max_bytes: int | None = None

    @property
    def transport_variable(self) -> str:
        return f"{self.variable}_B64"

    @property
    def transport_parameter(self) -> str:
        return f"{self.parameter}Base64"

    @property
    def transport_max_length(self) -> int | None:
        """Base64 length of ``max_bytes``: the ARM ``@maxLength`` of the transport."""
        if self.max_bytes is None:
            return None
        return 4 * -(-self.max_bytes // 3)


# `parameter` is the decoded Bicep value, the parameter name before this transport
# existed. The modules still receive a value under that name.
TRANSPORTS: tuple[Transport, ...] = (
    Transport("AI4IA_CLAUDE_BINDING_JSON", "claudeBindingJson"),
    Transport("AI4IA_PROXY_PROFILE_PROJECTION_JSON", "proxyProfileProjectionJson", secret=True),
    Transport("AI4IA_GROUP_POLICY_JSON", "groupPolicyJson", max_bytes=65536),
)


class TransportError(ValueError):
    """A value that ``encode`` could not have produced."""


def encode(raw: str) -> str:
    """Return the transport of ``raw``; the empty string stays empty."""
    try:
        data = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        # Undecodable environment bytes arrive as lone surrogates.
        raise TransportError("the value is not valid UTF-8 text") from exc
    return base64.b64encode(data).decode("ascii")


def decode(transport: str) -> str:
    """Strict inverse of ``encode``.

    Refuses whitespace, missing or extra padding, the URL-safe alphabet,
    nonzero padding bits and invalid UTF-8, so a transport has exactly one
    spelling and always round-trips.
    """
    try:
        raw = base64.b64decode(transport.encode("ascii"), validate=True).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise TransportError("not canonical UTF-8 base64") from exc
    if encode(raw) != transport:
        raise TransportError("not canonical UTF-8 base64")
    return raw
