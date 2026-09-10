"""Shared service inventory and image arguments for release and rollout gates."""

from __future__ import annotations

from collections.abc import Sequence

SERVICES = ("api", "web", "proxy")


class ImageInputError(ValueError):
    """The operator supplied an unsafe or unusable input."""


def parse_expected_images(values: Sequence[str]) -> dict[str, str]:
    """Parse rollout expectations, retaining support for historical tagged images."""
    expected: dict[str, str] = {}
    for raw in values:
        service, separator, reference = raw.partition("=")
        service = service.strip()
        reference = reference.strip()
        if not separator or not service or not reference:
            raise ImageInputError(
                f"--expect-image {raw!r} is not in SERVICE=REFERENCE form."
            )
        if service not in SERVICES:
            raise ImageInputError(
                f"--expect-image names unknown service {service!r}; "
                f"expected one of {', '.join(SERVICES)}."
            )
        if service in expected and expected[service] != reference:
            raise ImageInputError(
                f"--expect-image was given two different references for {service}."
            )
        expected[service] = reference
    return expected
