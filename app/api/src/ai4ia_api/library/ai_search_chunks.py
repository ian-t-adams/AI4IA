# pyright: reportArgumentType=false, reportCallIssue=false
# ^ Azure AI Search SDK typing friction, not real defects: SearchFieldDataType is
#   exposed as a plain Enum in the SDK's type stubs, so building SearchField(type=...)
#   and the SimpleField/SearchableField helpers trip reportArgumentType/reportCallIssue
#   even though the values are valid at runtime. Scoped to this Search backend module.
"""Azure AI Search backend for the per-user document chunk index.

A drop-in :class:`~ai4ia_api.library.doc_chunks.DocChunkStore` implementation that
indexes the library's CU-parsed chunks into an Azure AI Search index instead of
Postgres/pgvector. Selected by :mod:`ai4ia_api.library.ingest_factory` when
``search_endpoint`` is configured; otherwise pgvector / the in-memory store back
retrieval, so this is purely additive and dormant by default.

Design mirrors :class:`~ai4ia_api.library.doc_chunks.PgDocChunkStore`:

* **Keyless / AAD-only.** Reached over the global ``*.search.windows.net`` endpoint
  via the api managed identity (RBAC: *Search Index Data Contributor* +
  *Search Service Contributor*). No admin keys — ``DefaultAzureCredential`` only.
* **Lazy, idempotent bootstrap.** The index (HNSW + cosine vector profile over a
  ``memory_embedding_dimensions``-wide vector field) is created on first use via
  ``create_or_update_index`` under a single-flight lock.
* **Per-user isolation.** Each user gets their **own index**, named
  ``<prefix>-u<sha256(user_id)[:32]>`` (``search_index_per_user``, default on).
  The ``user_id`` filter is retained on every query regardless, so isolation
  never rests on index routing alone -- if resolution were ever wrong, the
  filter still refuses the read. Set ``AI4IA_SEARCH_INDEX_PER_USER=false`` for
  one shared index filtered the same way.

  The trade is explicit: an index-per-user service turns the tier's index limit
  into a ceiling on *users* (15 basic / 50 standard S1 / 200 S2-S3), and the
  user past it cannot upload at all. That failure is raised as
  :class:`SearchIndexQuotaError` rather than a bare 400, because the service's
  own message never mentions tenancy.
* **Hybrid + semantic retrieval.** When the caller passes ``query_text`` (RAG
  retrieval always does), ``search`` issues a *hybrid* query — the vector kNN
  alongside a BM25 keyword match over ``content`` — and, when ``semantic_ranking``
  is on, applies the L2 *semantic reranker* for materially better top-k ordering.
  A semantic failure (tier/quota unavailable) degrades gracefully to plain hybrid;
  with no ``query_text`` it is exactly the prior pure-vector search.
* **Backed-off semantic retries.** The semantic plan is a deployment parameter
  (``searchSemanticPlan`` in ``infra/modules/search.bicep``, default ``standard``
  — billed per query, no monthly cap). On the ``free`` plan it is capped at 1,000
  queries/month, and once that cap is hit every semantic query 403s, so retrying
  per request would make each retrieval pay a failed round-trip plus a stack
  trace before falling back — permanently, and silently. The breaker is kept on
  the paid plan because throttling and service errors produce the same shape:
  after a failure the store suppresses semantic for a cooldown that doubles up to
  an hour and clears on the first success, so a transient blip recovers in
  minutes and a hard outage costs at most one probe per hour instead of one per
  query.
* **Injectable clients.** The async ``SearchClient`` / ``SearchIndexClient`` and the
  credential can be injected so unit tests run with fakes and no live service.

The azure SDK is imported lazily so the app and tests run without it wired.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from .doc_chunks import DocChunkRecord

logger = logging.getLogger(__name__)


class SearchIndexQuotaError(RuntimeError):
    """The service is out of index slots.

    Index-per-user trades a per-tier ceiling on *users* for isolation: an
    Azure AI Search service allows 15 indexes on basic, 50 on standard (S1),
    and 200 on S2/S3. The user whose index would be number 51 cannot upload at
    all, and the raw service error ("cannot create more than N indexes") gives
    an operator no hint that it is the tenancy model talking. This names it.
    """


# An index name must be 2-128 chars, lowercase, start with a letter or digit, and
# use only letters/digits/dashes/underscores with no consecutive dashes or
# underscores. The per-user suffix is a hex digest, so it satisfies all of that by
# construction; the operator-supplied prefix does not, and is validated.
_INDEX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,126}[a-z0-9]$")
_INDEX_NAME_MAX = 128
# Length of the hashed user token in the index name. 32 hex chars is 128 bits of
# the digest -- collision-free for any realistic user count, and short enough to
# leave the prefix room inside the 128-char ceiling.
_USER_TOKEN_LEN = 32


def user_index_token(user_id: str) -> str:
    """Deterministic, always-name-safe token for a user's index.

    Hashed rather than sanitized for two reasons: an internal user id has no
    guaranteed shape, so sanitizing could collide two distinct users onto one
    index (a cross-tenant read, which is the one failure this whole design
    exists to prevent), and an index name is visible to anyone with control-plane
    read on the service, so it should not carry an identifier.
    """
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return digest[:_USER_TOKEN_LEN]


def index_name_for(prefix: str, user_id: str) -> str:
    """The index name backing ``user_id``, or raise if it would be invalid."""
    name = f"{prefix}-u{user_index_token(user_id)}"
    if len(name) > _INDEX_NAME_MAX or not _INDEX_NAME_RE.fullmatch(name):
        raise ValueError(
            f"index prefix {prefix!r} yields an invalid Azure AI Search index "
            f"name {name!r}: names are 2-{_INDEX_NAME_MAX} chars, lowercase, "
            "start with a letter or digit, and use only letters, digits, dashes "
            "and underscores with no consecutive dashes or underscores."
        )
    return name


def _is_index_quota_error(exc: BaseException) -> bool:
    """Whether ``exc`` is the service refusing to create another index.

    Matched on message text because the SDK surfaces it as a generic 400
    ``HttpResponseError`` with no distinguishing code. Deliberately narrow: a
    false positive here would relabel an unrelated 400 as a capacity problem and
    send an operator to the wrong runbook.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status not in (400, 403):
        return False
    text = str(exc).lower()
    return "index" in text and any(
        marker in text
        for marker in ("quota", "maximum number", "cannot create more", "limit")
    )


_VECTOR_FIELD = "embedding"
_VECTOR_PROFILE = "vprofile"
_HNSW_CONFIG = "hnsw"
# Semantic (L2 reranker) configuration name. Defined on the index unconditionally
# (harmless when unused); a query opts into it via ``query_type="semantic"`` when
# semantic ranking is enabled and the query carries text.
_SEMANTIC_CONFIG = "ai4ia-semantic"
# Cooldown applied after a failed semantic query, doubling per consecutive failure
# up to the cap. Bounds the cost of a permanently unavailable reranker (an
# exhausted free-plan quota lasts until the month rolls over) while still letting a
# transient failure recover on its own within minutes.
_SEMANTIC_COOLDOWN_INITIAL_S = 300.0
_SEMANTIC_COOLDOWN_MAX_S = 3600.0
# AI Search caps an index batch at 1000 documents.
_UPLOAD_BATCH = 1000
# Fields read back on a query (the embedding vector is deliberately omitted — it is
# large and never needed downstream; ``@search.score`` is always returned).
_SELECT_FIELDS = (
    "chunk_id",
    "user_id",
    "document_id",
    "chunk_index",
    "content",
    "heading",
    "char_start",
    "char_end",
    "start_ms",
    "end_ms",
    "speaker",
    "created_at",
)


def _indexing_succeeded(result: Any) -> bool:
    if isinstance(result, dict):
        succeeded = result.get("succeeded")
        if succeeded is None:
            succeeded = result.get("status")
        return succeeded is True
    return getattr(result, "succeeded", None) is True


def _indexing_failure_detail(result: Any) -> str:
    if isinstance(result, dict):
        key = result.get("key")
        status_code = result.get("status_code")
        error = result.get("error_message") or result.get("error")
    else:
        key = getattr(result, "key", None)
        status_code = getattr(result, "status_code", None)
        error = getattr(result, "error_message", None)
    return f"key={key!r} status={status_code!r} error={str(error or '')[:200]!r}"


def _encode_key(raw: str) -> str:
    """Encode a chunk id into a valid AI Search document key.

    Search keys allow only letters, digits, ``_``, ``-`` and ``=``; our ids are
    ``{user_id}:{document_id}:{chunk_index}`` which can carry colons and other
    characters. url-safe base64 (alphabet ``A-Za-z0-9-_`` + ``=`` padding) is a
    reversible mapping entirely inside the allowed set.
    """
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_key(key: str) -> str:
    try:
        return base64.urlsafe_b64decode(key.encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001 - a malformed key should never break a read
        return key


def _odata_escape(value: str) -> str:
    """Escape a string literal for an OData ``$filter`` (single quote doubling)."""
    return str(value).replace("'", "''")


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class AzureSearchDocChunkStore:
    """Per-user doc-chunk store backed by an Azure AI Search index."""

    def __init__(
        self,
        *,
        endpoint: str,
        index_name: str,
        expected_dim: int,
        semantic_ranking: bool = True,
        per_user_index: bool = True,
        credential: Any | None = None,
        search_client: Any | None = None,
        index_client: Any | None = None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        self._endpoint = endpoint
        # With ``per_user_index`` this is a *prefix*; otherwise it is the literal
        # shared index name.
        self._index_name = index_name
        self._per_user_index = per_user_index
        self._expected_dim = expected_dim
        self._semantic_ranking = semantic_ranking
        self._credential = credential
        self._owns_credential = credential is None
        # An injected client is a test seam and serves every user: routing by
        # index name only applies to clients this store builds itself.
        self._injected_search_client = search_client
        self._search_clients: dict[str, Any] = {}
        self._index_client = index_client
        self._owns_index_client = index_client is None
        # Index names known to exist. Per-user means "created once per user",
        # not once per process, so a bool would let user 2 skip the creation
        # user 1 performed and query an index that does not exist yet.
        self._ready_indexes: set[str] = set()
        self._init_lock = asyncio.Lock()
        # Monotonic clock, injectable so the backoff is tested without sleeping.
        self._now = time_source or time.monotonic
        # Deadline before which semantic rerank is suppressed (None => allowed),
        # and the cooldown to apply on the next failure.
        self._semantic_retry_at: float | None = None
        self._semantic_cooldown_s = _SEMANTIC_COOLDOWN_INITIAL_S

    def index_name_for_user(self, user_id: str) -> str:
        """The index backing ``user_id`` under the configured tenancy model."""
        if not self._per_user_index:
            return self._index_name
        return index_name_for(self._index_name, user_id)

    # --- lazy client construction -------------------------------------------------
    async def _get_credential(self) -> Any:
        if self._credential is None:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            self._owns_credential = True
        return self._credential

    async def _get_search_client(self, user_id: str) -> Any:
        if self._injected_search_client is not None:
            return self._injected_search_client
        name = self.index_name_for_user(user_id)
        client = self._search_clients.get(name)
        if client is None:
            from azure.search.documents.aio import SearchClient

            credential = await self._get_credential()
            client = SearchClient(
                endpoint=self._endpoint,
                index_name=name,
                credential=credential,
            )
            self._search_clients[name] = client
        return client

    async def _get_index_client(self) -> Any:
        if self._index_client is None:
            from azure.search.documents.indexes.aio import SearchIndexClient

            credential = await self._get_credential()
            self._index_client = SearchIndexClient(
                endpoint=self._endpoint, credential=credential
            )
            self._owns_index_client = True
        return self._index_client

    # --- index bootstrap ----------------------------------------------------------
    def _build_index(self, name: str) -> Any:
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            HnswParameters,
            SearchableField,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SemanticConfiguration,
            SemanticField,
            SemanticPrioritizedFields,
            SemanticSearch,
            SimpleField,
            VectorSearch,
            VectorSearchAlgorithmMetric,
            VectorSearchProfile,
        )

        fields = [
            SimpleField(name="key", type=SearchFieldDataType.String, key=True),
            SimpleField(name="chunk_id", type=SearchFieldDataType.String),
            SimpleField(name="user_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(
                name="document_id", type=SearchFieldDataType.String, filterable=True
            ),
            SimpleField(
                name="chunk_index", type=SearchFieldDataType.Int32, sortable=True
            ),
            SearchableField(name="content", type=SearchFieldDataType.String),
            SimpleField(name="heading", type=SearchFieldDataType.String),
            SimpleField(name="char_start", type=SearchFieldDataType.Int32),
            SimpleField(name="char_end", type=SearchFieldDataType.Int32),
            SimpleField(name="start_ms", type=SearchFieldDataType.Int32),
            SimpleField(name="end_ms", type=SearchFieldDataType.Int32),
            SimpleField(name="speaker", type=SearchFieldDataType.String),
            SimpleField(name="created_at", type=SearchFieldDataType.DateTimeOffset),
            SearchField(
                name=_VECTOR_FIELD,
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=self._expected_dim,
                vector_search_profile_name=_VECTOR_PROFILE,
            ),
        ]
        vector_search = VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name=_HNSW_CONFIG,
                    parameters=HnswParameters(metric=VectorSearchAlgorithmMetric.COSINE),
                )
            ],
            profiles=[
                VectorSearchProfile(
                    name=_VECTOR_PROFILE,
                    algorithm_configuration_name=_HNSW_CONFIG,
                )
            ],
        )
        # ``content`` is the only searchable field, so it is the semantic content
        # field. Adding the configuration to an existing index is a non-destructive
        # update (no field-attribute change, no rebuild).
        semantic_search = SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name=_SEMANTIC_CONFIG,
                    prioritized_fields=SemanticPrioritizedFields(
                        content_fields=[SemanticField(field_name="content")]
                    ),
                )
            ]
        )
        return SearchIndex(
            name=name,
            fields=fields,
            vector_search=vector_search,
            semantic_search=semantic_search,
        )

    async def ensure_ready(self, user_id: str) -> None:
        """Create this user's index if it does not exist yet (idempotent)."""
        name = self.index_name_for_user(user_id)
        if name in self._ready_indexes:
            return
        async with self._init_lock:
            if name in self._ready_indexes:
                return
            index_client = await self._get_index_client()
            try:
                await index_client.create_or_update_index(self._build_index(name))
            except Exception as exc:  # noqa: BLE001 - re-raised, possibly relabelled
                if _is_index_quota_error(exc):
                    raise SearchIndexQuotaError(
                        "Azure AI Search has no index slots left, so this user's "
                        f"document index ({name}) could not be created. With "
                        "per-user indexes the tier's index limit is a ceiling on "
                        "users (15 on basic, 50 on standard S1, 200 on S2/S3). "
                        "Raise the tier or move to a shared index "
                        "(AI4IA_SEARCH_INDEX_PER_USER=false)."
                    ) from exc
                raise
            self._ready_indexes.add(name)

    # --- helpers ------------------------------------------------------------------
    def _check_dim(self, vector: Sequence[float]) -> None:
        if self._expected_dim is not None and len(vector) != self._expected_dim:
            raise ValueError(
                f"embedding dimension {len(vector)} != expected {self._expected_dim}"
            )

    def _to_document(
        self, record: DocChunkRecord, vector: Sequence[float]
    ) -> dict[str, Any]:
        return {
            "key": _encode_key(record.id),
            "chunk_id": record.id,
            "user_id": record.user_id,
            "document_id": record.document_id,
            "chunk_index": int(record.chunk_index),
            "content": record.content,
            "heading": record.heading,
            "char_start": record.char_start,
            "char_end": record.char_end,
            "start_ms": record.start_ms,
            "end_ms": record.end_ms,
            "speaker": record.speaker,
            "created_at": record.created_at.isoformat(),
            "embedding": [float(x) for x in vector],
        }

    def _from_document(self, doc: Any) -> DocChunkRecord:
        raw_id = doc.get("chunk_id") or _decode_key(doc.get("key", ""))
        # Prefer the semantic reranker score (0-4) when present so the record's
        # score reflects the final ordering; fall back to the search/RRF score.
        score = doc.get("@search.reranker_score")
        if score is None:
            score = doc.get("@search.score")
        return DocChunkRecord(
            user_id=doc.get("user_id", ""),
            document_id=doc.get("document_id", ""),
            chunk_index=int(doc.get("chunk_index") or 0),
            content=doc.get("content") or "",
            heading=doc.get("heading"),
            char_start=doc.get("char_start"),
            char_end=doc.get("char_end"),
            start_ms=doc.get("start_ms"),
            end_ms=doc.get("end_ms"),
            speaker=doc.get("speaker"),
            id=raw_id,
            created_at=_parse_dt(doc.get("created_at")),
            score=float(score) if score is not None else None,
        )

    def _build_filter(
        self, user_id: str, document_ids: Sequence[str] | None
    ) -> str:
        parts = [f"user_id eq '{_odata_escape(user_id)}'"]
        if document_ids:
            clauses = " or ".join(
                f"document_id eq '{_odata_escape(d)}'" for d in document_ids
            )
            parts.append(f"({clauses})")
        return " and ".join(parts)

    # --- semantic rerank availability ---------------------------------------------
    def _semantic_allowed(self) -> bool:
        """Whether to attempt a semantic query now (False while backing off)."""
        if self._semantic_retry_at is None:
            return True
        if self._now() >= self._semantic_retry_at:
            return True
        return False

    @staticmethod
    def _is_service_level_failure(exc: BaseException) -> bool:
        """Whether a semantic failure is about the *service*, not this query.

        The breaker is process-wide, so it must only open for conditions that
        affect everyone -- an exhausted semantic plan (403), throttling (429),
        service errors, or transport failures. A request-specific rejection
        (typically 400 for a malformed query) still falls back to hybrid for that
        caller, but must not downgrade unrelated users for an hour.
        """
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
            return True  # transport/unknown: treat as a service problem
        if status in {403, 408, 429}:
            return True
        return status >= 500

    def _trip_semantic(self) -> None:
        """Record a semantic failure and suppress further attempts for a while.

        Logged at WARNING with the traceback exactly once per trip: repeating it
        per request is what made an exhausted quota fill the logs. Concurrent
        failures from a single outage wave advance the backoff once, not once per
        in-flight query.
        """
        already_open = (
            self._semantic_retry_at is not None
            and self._now() < self._semantic_retry_at
        )
        if already_open:
            return
        cooldown = self._semantic_cooldown_s
        self._semantic_retry_at = self._now() + cooldown
        self._semantic_cooldown_s = min(cooldown * 2, _SEMANTIC_COOLDOWN_MAX_S)
        logger.warning(
            "AI Search semantic rerank unavailable; falling back to hybrid and "
            "suppressing semantic for %.0fs",
            cooldown,
            exc_info=True,
        )

    def _reset_semantic(self) -> None:
        """Clear the backoff after a semantic query succeeds."""
        if (
            self._semantic_retry_at is None
            and self._semantic_cooldown_s == _SEMANTIC_COOLDOWN_INITIAL_S
        ):
            return
        self._semantic_retry_at = None
        self._semantic_cooldown_s = _SEMANTIC_COOLDOWN_INITIAL_S
        logger.info("AI Search semantic rerank recovered; resuming semantic queries")

    # --- DocChunkStore protocol ---------------------------------------------------
    async def add_many(
        self, records: Sequence[DocChunkRecord], vectors: Sequence[Sequence[float]]
    ) -> None:
        if len(records) != len(vectors):
            raise ValueError("records and vectors length mismatch")
        if not records:
            return
        for vector in vectors:
            self._check_dim(vector)
        # Every record must belong to one user, because the user selects the
        # index. A mixed batch was harmless when one shared index held everyone;
        # now it would silently write one user's chunks into another's index,
        # which is the exact cross-tenant leak this tenancy model exists to make
        # impossible. Callers already write one document at a time.
        owners = {record.user_id for record in records}
        if len(owners) != 1:
            raise ValueError(
                f"add_many requires records from exactly one user, got {len(owners)}"
            )
        user_id = owners.pop()
        await self.ensure_ready(user_id)
        client = await self._get_search_client(user_id)
        documents = [self._to_document(r, v) for r, v in zip(records, vectors)]
        for start in range(0, len(documents), _UPLOAD_BATCH):
            batch = documents[start : start + _UPLOAD_BATCH]
            results = list(
                await client.merge_or_upload_documents(documents=batch)
            )
            failed = [result for result in results if not _indexing_succeeded(result)]
            if len(results) != len(batch) or failed:
                detail = "; ".join(_indexing_failure_detail(result) for result in failed[:3])
                raise RuntimeError(
                    "Azure AI Search indexed "
                    f"{len(results) - len(failed)}/{len(batch)} document chunks"
                    + (f": {detail}" if detail else "")
                )

    async def _collect(self, search_awaitable: Any) -> list[DocChunkRecord]:
        """Await a ``client.search(...)`` call and map its async results in order."""
        results = await search_awaitable
        out: list[DocChunkRecord] = []
        async for doc in results:
            out.append(self._from_document(doc))
        return out

    async def search(
        self,
        user_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        document_ids: Sequence[str] | None = None,
        query_text: str | None = None,
    ) -> list[DocChunkRecord]:
        self._check_dim(query_vector)
        k = max(0, top_k)
        if k == 0:
            return []
        if document_ids is not None:
            ids = list(document_ids)
            if not ids:
                return []
            document_ids = ids
        await self.ensure_ready(user_id)

        from azure.search.documents.models import VectorizedQuery

        vector_query = VectorizedQuery(
            vector=[float(x) for x in query_vector],
            k_nearest_neighbors=k,
            fields=_VECTOR_FIELD,
        )
        client = await self._get_search_client(user_id)
        # A non-empty query text turns the pure-vector kNN into a *hybrid* query
        # (vector + BM25 over ``content``). ``None`` preserves the prior pure-vector
        # behavior. Azure AI Search requires ``search_text=None`` (not "") for a
        # vector-only query.
        text = (query_text or "").strip() or None
        base_kwargs: dict[str, Any] = {
            "vector_queries": [vector_query],
            "filter": self._build_filter(user_id, document_ids),
            "top": k,
            "select": list(_SELECT_FIELDS),
        }
        # Hybrid + semantic L2 rerank when enabled and the query carries text. If the
        # semantic tier is unavailable (SKU/quota), degrade to plain hybrid rather
        # than failing the retrieval turn, and back off so the next queries don't
        # each pay a doomed round-trip.
        if text is not None and self._semantic_ranking and self._semantic_allowed():
            try:
                results = await self._collect(
                    client.search(
                        search_text=text,
                        query_type="semantic",
                        semantic_configuration_name=_SEMANTIC_CONFIG,
                        **base_kwargs,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - degrade to hybrid on failure
                if self._is_service_level_failure(exc):
                    self._trip_semantic()
                else:
                    logger.warning(
                        "AI Search semantic query rejected; falling back to "
                        "hybrid for this query only",
                        exc_info=True,
                    )
            else:
                self._reset_semantic()
                return results
        return await self._collect(client.search(search_text=text, **base_kwargs))

    async def delete_document(self, user_id: str, document_id: str) -> int:
        await self.ensure_ready(user_id)
        client = await self._get_search_client(user_id)
        filter_str = (
            f"user_id eq '{_odata_escape(user_id)}' "
            f"and document_id eq '{_odata_escape(document_id)}'"
        )
        results = await client.search(
            search_text="*", filter=filter_str, select=["key"]
        )
        keys: list[str] = []
        async for doc in results:
            key = doc.get("key")
            if key:
                keys.append(key)
        if not keys:
            return 0
        deleted = 0
        for start in range(0, len(keys), _UPLOAD_BATCH):
            batch = [{"key": key} for key in keys[start : start + _UPLOAD_BATCH]]
            delete_results = list(await client.delete_documents(documents=batch))
            failed = [
                result
                for result in delete_results
                if not _indexing_succeeded(result)
            ]
            if len(delete_results) != len(batch) or failed:
                detail = "; ".join(
                    _indexing_failure_detail(result) for result in failed[:3]
                )
                raise RuntimeError(
                    "Azure AI Search deleted "
                    f"{len(delete_results) - len(failed)}/{len(batch)} document chunks"
                    + (f": {detail}" if detail else "")
                )
            deleted += len(delete_results)
        return deleted

    async def close(self) -> None:
        # Per-user routing means N clients, not one. Closing only the most
        # recent would leak a connection pool per user for the process lifetime.
        for client in list(self._search_clients.values()):
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning("search client close failed", exc_info=True)
        self._search_clients.clear()
        if self._owns_index_client and self._index_client is not None:
            await self._index_client.close()
            self._index_client = None
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()
            self._credential = None
        self._ready_indexes.clear()
