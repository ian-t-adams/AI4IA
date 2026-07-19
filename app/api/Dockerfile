# AI4IA backend image (Python FastAPI on Azure Container Apps).
# Build context is app/api (see azure.yaml); the model catalog is bundled into
# the package by scripts/gen-model-catalog.py before build.
# Keep this version in sync with app-ci.yml's actions/setup-python version and
# the invariant documented in AGENTS.md ("CI uses Python 3.12"). Bump deliberately
# alongside a CI version update (and note the reason here), not via an unreviewed
# Dependabot major-version merge: see AI4IA#187 for a base-image drift that reached
# python:3.14-slim untested and caused azure-cosmos/aiohttp DeprecationWarning noise
# in production before anything exercised that combination.
FROM python:3.12-slim AS base

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
