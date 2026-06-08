# AI4IA backend image (Python FastAPI on Azure Container Apps).
# Build context is app/api (see azure.yaml); the model catalog is bundled into
# the package by scripts/gen-model-catalog.py before build.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

EXPOSE 8080

# Non-root runtime.
RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["sh", "-c", "uvicorn ai4ia_api.main:app --host 0.0.0.0 --port ${PORT}"]
