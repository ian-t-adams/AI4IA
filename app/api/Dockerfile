# AI4IA backend image (Python FastAPI on Azure Container Apps).
# Build context is app/api (see azure.yaml); the model catalog is bundled into
# the package by scripts/gen-model-catalog.py before build.
#
# ---------------------------------------------------------------------------
# Base image is pinned to an immutable DIGEST, not just the moving `3.12-slim`
# tag (audit finding P1-7). A tag is a mutable pointer: docker-build.yml
# resolves it when the PR runs and `azd deploy` resolves it again later, so the
# image CI proved and the image production runs can be different builds of
# different Python patch releases with no diff anywhere in the repo to show it.
# This is not theoretical here -- `docker.io/library/python:3.12-slim` moved to
# the digest below on 2026-08-05, one day before this pin was taken. With the
# digest present, the two builds either use the same bytes or the pull fails.
#
# The TAG is kept in the reference alongside the digest on purpose: it is what
# Dependabot's docker ecosystem tracks (its `FROM_LINE` regex parses
# `image:tag@sha256:...` and `UpdateChecker#updated_requirements` rewrites the
# tag and the digest together), and it keeps major.minor legible to a human.
#
# Do NOT assume the digest keeps itself fresh. dependabot-core suppresses a
# digest-only refresh of an UNCHANGED comparable tag behind the server-side
# `docker_digest_only_update_suppression` experiment, whose state is not
# observable from this repo -- and `3.12-slim` is a floating tag whose NAME
# never changes, so if that experiment is on, no PR is ever opened here.
# Treat refreshing this digest as a manual audit step.
#
# Currently sha256:646fb0bc... = the multi-platform OCI image index for
# python 3.12.13 (linux/amd64, arm64/v8, 386, arm/v5, arm/v7, ppc64le,
# riscv64, s390x), published 2026-08-05 and pinned 2026-08-06.
#
# To refresh:
#   docker buildx imagetools inspect python:3.12-slim
# and take the `Digest:` line. That is the INDEX digest; do not substitute a
# per-platform manifest digest or the build breaks on every other
# architecture. A wrong digest fails docker-build.yml on the PR that
# introduces it; scripts/tests/test_base_image_pins.py fails if the pin is
# dropped altogether.
#
# Keep the MAJOR.MINOR in sync with app-ci.yml's actions/setup-python version
# and the invariant documented in AGENTS.md ("CI uses Python 3.12"). Bump it
# deliberately alongside a CI version update (and note the reason here), not
# via an unreviewed Dependabot major-version merge: see AI4IA#187 for a
# base-image drift that reached python:3.14-slim without an image test.
# Production warning reports prompted the review, but the audit did not
# establish that Python 3.14 caused those warnings.
# ---------------------------------------------------------------------------
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
# '.' is this repo's own package (its dependencies are pinned in pyproject.toml),
# not a PyPI package to version-pin in the Dockerfile.
# hadolint ignore=DL3013
RUN pip install --upgrade pip && pip install .

EXPOSE 8080

# Non-root runtime.
RUN useradd --create-home --uid 10001 appuser
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/health/live', timeout=3).read()"

CMD ["sh", "-c", "uvicorn ai4ia_api.main:app --host 0.0.0.0 --port ${PORT}"]
