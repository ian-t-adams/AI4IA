"""Exact-endpoint, bounded read transport for the operator-only assessment.

The supervisor kills a stuck credential/SDK read, retaining earlier observations.
The real Cosmos SDK must serialize an allowed projection and an exact selected
partition before the transport can send it. No point reads or mutation verbs.
"""

from __future__ import annotations

import importlib.metadata
import logging
import multiprocessing
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from multiprocessing.connection import Connection
from typing import Any
from urllib.parse import urlsplit

import httpx
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport
from azure.cosmos import CosmosClient

from _deletion_assessment import (
    COLLECTION_SECONDS,
    COSMOS_ARM_API,
    COSMOS_DATA_API,
    MAX_CALLS,
    MAX_PAGE_BYTES,
    MAX_TOTAL_BYTES,
    PAGE_ITEMS,
    SOURCE_SECONDS,
    STORAGE_ARM_API,
    SURFACES,
    AssessmentError,
    MetadataSource,
    Page,
    Query,
    Scope,
    Surface,
    account_layout,
    canonical,
    container_layout,
    cursor,
    obj,
    strict_json,
)

ARM = "https://management.azure.com"


class _EmptyPageBoundary(Exception):
    """Stop SDK auto-advance after one observed empty, nonterminal response."""


class Wire:
    def __init__(self, client: httpx.Client, *, clock=time.monotonic):
        self.client, self.clock = client, clock
        self.started = clock()
        self.calls = 0
        self.received = 0

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> tuple[bytes, dict[str, str]]:
        if self.received >= MAX_TOTAL_BYTES:
            raise AssessmentError("response_byte_limit")
        if self.calls >= MAX_CALLS:
            raise AssessmentError("transport_call_limit")
        remaining = COLLECTION_SECONDS - (self.clock() - self.started)
        if remaining <= 0:
            raise AssessmentError("collection_timeout")
        self.calls += 1
        started = self.clock()
        timeout = min(SOURCE_SECONDS, remaining)
        safe_headers = {k: v for k, v in headers.items() if k.lower() not in ("cookie", "accept-encoding")}
        safe_headers["Accept-Encoding"] = "identity"
        self.client.cookies.clear()
        try:
            with self.client.stream(
                method, url, headers=safe_headers, content=body, timeout=timeout, follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise AssessmentError("source_not_found" if response.status_code == 404 else "source_read_failed")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise AssessmentError("encoded_response_refused")
                length = response.headers.get("content-length")
                if length is not None and (
                    not length.isascii() or not length.isdecimal()
                    or int(length) > min(MAX_PAGE_BYTES, MAX_TOTAL_BYTES - self.received)
                ):
                    raise AssessmentError("response_byte_limit")
                chunks = []
                size = 0
                for chunk in response.iter_raw():
                    size += len(chunk)
                    self.received += len(chunk)
                    if size > MAX_PAGE_BYTES or self.received > MAX_TOTAL_BYTES:
                        raise AssessmentError("response_byte_limit")
                    if self.clock() - started >= timeout:
                        raise AssessmentError("source_timeout")
                    chunks.append(chunk)
                if self.clock() - started >= timeout:
                    raise AssessmentError("source_timeout")
                return b"".join(chunks), dict(response.headers)
        except httpx.TimeoutException:
            raise AssessmentError("source_timeout") from None
        except httpx.HTTPError:
            raise AssessmentError("source_read_failed") from None


class BufferedResponse(HttpResponse):
    def __init__(self, request: HttpRequest, body: bytes, headers: dict[str, str]):
        super().__init__(request, None)
        self.status_code = 200
        self.headers = headers
        self.reason = "OK"
        self.content_type = headers.get("content-type", "application/json")
        self._body = body

    def body(self) -> bytes:
        return self._body


class ReadOnlyTransport(HttpTransport):
    def __init__(self, scope: Scope, wire: Wire):
        self.scope, self.wire = scope, wire
        self.expected: Query | None = None
        self.continuation: str | None = None
        self.posts = 0
        self.observed_page: Page | None = None
        self.layouts: dict[str, bool] = {}

    def open(self) -> None:
        pass

    def close(self) -> None:
        # The owner closes the shared ARM/Cosmos HTTP client, not this adapter.
        pass

    def __enter__(self) -> ReadOnlyTransport:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def sleep(self, duration: float) -> None:
        raise AssessmentError("sdk_retry_refused")

    def send(self, request: HttpRequest, **_kwargs: Any) -> HttpResponse:
        url = urlsplit(request.url)
        expected_host = urlsplit(self.scope.endpoint).hostname
        if (
            url.scheme != "https" or url.hostname != expected_host or url.port not in (None, 443)
            or url.username or url.password or url.query or url.fragment
        ):
            raise AssessmentError("transport_scope_refused")
        headers = {k.lower(): str(v) for k, v in request.headers.items()}
        if headers.get("x-ms-version") != COSMOS_DATA_API:
            raise AssessmentError("unsupported_cosmos_api")
        base = f"/dbs/{self.scope.database}/colls/"
        body = None
        if request.method == "GET" and url.path.rstrip("/") in (
            "", *(base + surface for surface in SURFACES),
        ):
            if request.body not in (None, "", b""):
                raise AssessmentError("transport_scope_refused")
        elif request.method == "POST" and self.expected is not None:
            query = self.expected
            query.validate(self.scope)
            if url.path.rstrip("/") != base + query.surface + "/docs":
                raise AssessmentError("transport_scope_refused")
            if self.layouts.get(query.surface) is not True:
                raise AssessmentError("partition_layout_not_observed")
            body = request.body.encode("utf-8") if isinstance(request.body, str) else request.body
            if not isinstance(body, bytes) or len(body) > MAX_PAGE_BYTES:
                raise AssessmentError("query_projection_refused")
            payload = strict_json(body)
            if payload != {"query": query.sql, "parameters": query.parameters}:
                raise AssessmentError("query_projection_refused")
            partition = strict_json(headers.get("x-ms-documentdb-partitionkey", "null").encode("utf-8"))
            if partition != [query.partition]:
                raise AssessmentError("query_partition_refused")
            if (
                headers.get("x-ms-documentdb-isquery") != "true"
                or headers.get("x-ms-documentdb-query-enablecrosspartition", "false") != "false"
                or headers.get("x-ms-max-item-count") != str(PAGE_ITEMS)
                or any(key in headers for key in (
                    "x-ms-documentdb-pre-trigger-include", "x-ms-documentdb-post-trigger-include",
                    "x-ms-cosmos-is-query-plan-request", "x-ms-cosmos-allow-tentative-writes",
                ))
            ):
                raise AssessmentError("query_options_refused")
            if self.posts:
                observed = self.observed_page
                if (
                    self.posts == 1 and observed is not None and not observed.rows
                    and observed.continuation is not None
                    and headers.get("x-ms-continuation") == observed.continuation
                ):
                    # The SDK coalesces empty pages internally. Yield the already
                    # received page to our bounded collector BEFORE another send,
                    # without inventing emptiness from an error or changing bytes.
                    raise _EmptyPageBoundary()
                raise AssessmentError("sdk_replay_refused")
            if headers.get("x-ms-continuation") != self.continuation:
                raise AssessmentError("query_options_refused")
            self.posts += 1
        else:
            raise AssessmentError("transport_operation_refused")
        raw, response_headers = self.wire.request(request.method, request.url, request.headers, body)
        value = obj(strict_json(raw))
        if request.method == "GET":
            if not url.path.rstrip("/"):
                locations = value.get("writableLocations")
                if (
                    not isinstance(locations, list) or len(locations) != 1
                    or value.get("enableMultipleWriteLocations") is not False
                    or obj(value.get("userConsistencyPolicy")).get("defaultConsistencyLevel") != "Session"
                ):
                    raise AssessmentError("cosmos_account_layout_disagrees")
            else:
                observed_surface: Surface = next(s for s in SURFACES if url.path.rstrip("/") == base + s)
                self.layouts[observed_surface] = container_layout(value, observed_surface)["status"] == "compatible"
        if request.method == "POST":
            rows = value.get("Documents")
            if not isinstance(rows, list) or len(rows) > PAGE_ITEMS:
                raise AssessmentError("invalid_query_page")
            if type(value.get("_count")) is not int or value["_count"] != len(rows):
                raise AssessmentError("invalid_query_count")
            token = response_headers.get("x-ms-continuation")
            self.observed_page = Page(rows, cursor(None if token == "" else token))
        return BufferedResponse(request, raw, response_headers)


class SdkReader:
    def __init__(self, scope: Scope, credential: TokenCredential, wire: Wire):
        self.scope, self.credential, self.wire = scope, credential, wire
        self.transport = ReadOnlyTransport(scope, wire)
        self.client: CosmosClient | None = None
        self.account_bound = False

    def _cosmos(self) -> CosmosClient:
        if not self.account_bound:
            raise AssessmentError("account_binding_required")
        if self.client is None:
            self.client = CosmosClient(
                self.scope.endpoint, credential=self.credential, transport=self.transport,
                enable_endpoint_discovery=False, enable_diagnostics_logging=False,
                logging_enable=False, retry_total=0, retry_connect=0, retry_read=0,
                retry_status=0, connection_timeout=SOURCE_SECONDS, read_timeout=SOURCE_SECONDS,
                availability_strategy=False,
            )
        return self.client

    def _arm(self, path: str, version: str) -> dict[str, Any]:
        token = self.credential.get_token("https://management.azure.com/.default")
        body, _ = self.wire.request(
            "GET", f"{ARM}{path}?api-version={version}",
            {"Authorization": "Bearer " + token.token, "Accept": "application/json"},
        )
        return obj(strict_json(body))

    def metadata(self, source: MetadataSource) -> dict[str, Any]:
        if source == "account":
            raw = self._arm(self.scope.account_id, COSMOS_ARM_API)
            properties = obj(raw.get("properties"))
            result = {
                "id": raw.get("id"),
                "sdkVersion": importlib.metadata.version("azure-cosmos"),
                "properties": {key: properties[key] for key in (
                    "documentEndpoint", "writeLocations", "enableMultipleWriteLocations",
                    "consistencyPolicy", "backupPolicy",
                ) if key in properties},
            }
            # Bind the exact ARM identity before constructing the SDK, including
            # its implicit account lookup. Incompatible posture can still be
            # observed, but an unbound endpoint cannot be queried.
            account_layout(result, self.scope)
            self.account_bound = True
            return result
        if source in SURFACES:
            raw = self._cosmos().get_database_client(self.scope.database).get_container_client(source).read()
            return {key: raw[key] for key in (
                "id", "partitionKey", "defaultTtl", "analyticalStorageTtl",
            ) if key in raw}
        if source in ("blob_service", "blob_container") and self.scope.blob_id is not None:
            path = self.scope.blob_id if source == "blob_container" else self.scope.blob_id.rsplit("/containers/", 1)[0]
            raw = self._arm(path, STORAGE_ARM_API)
            properties = obj(raw.get("properties"))
            fields = (
                ("isVersioningEnabled", "deleteRetentionPolicy", "containerDeleteRetentionPolicy")
                if source == "blob_service" else ("hasLegalHold", "hasImmutabilityPolicy")
            )
            return {"id": raw.get("id"), "properties": {key: properties[key] for key in fields if key in properties}}
        raise AssessmentError("out_of_scope")

    def page(self, query: Query, continuation: str | None) -> Page:
        query.validate(self.scope)
        self.transport.expected, self.transport.continuation = query, cursor(continuation)
        self.transport.posts = 0
        self.transport.observed_page = None
        try:
            container = self._cosmos().get_database_client(self.scope.database).get_container_client(query.surface)
            iterator = container.query_items(
                query=query.sql, parameters=query.parameters, partition_key=query.partition,
                enable_cross_partition_query=False, max_item_count=PAGE_ITEMS,
                populate_query_metrics=False, populate_index_metrics=False,
                continuation_token_limit=4,
            ).by_page(continuation_token=continuation)
            try:
                rows = list(next(iterator))
            except (StopIteration, _EmptyPageBoundary):
                # Cosmos 4.x suppresses empty pages, even when the response has a
                # continuation. Only the request-local transport observation can
                # distinguish this from no response. Do not use global headers.
                rows = []
            observed = self.transport.observed_page
            if self.transport.posts != 1 or observed is None:
                raise AssessmentError("query_not_observed")
            if rows != observed.rows:
                raise AssessmentError("sdk_page_mismatch")
            return observed
        finally:
            self.transport.expected = None
            self.transport.continuation = None

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def _serve(connection: Connection, private_scope: dict[str, Any]) -> None:
    # This child is created only by explicit collect. No diagnostics can emit
    # endpoints, credential-chain details, SDK response bodies or exception text.
    logging.disable(logging.CRITICAL)
    try:
        from azure.identity import DefaultAzureCredential

        scope = Scope.parse(private_scope)
        with DefaultAzureCredential(exclude_interactive_browser_credential=True, process_timeout=10) as credential:
            with httpx.Client(trust_env=False, follow_redirects=False) as client:
                reader = SdkReader(scope, credential, Wire(client))
                try:
                    while True:
                        request = obj(strict_json(connection.recv_bytes(MAX_PAGE_BYTES)))
                        operation = request.get("operation")
                        if operation == "close":
                            return
                        try:
                            if operation == "metadata":
                                result = reader.metadata(request["source"])
                            elif operation == "page":
                                index = request["cohortIndex"]
                                if type(index) is not int or not 0 <= index < len(scope.cohort):
                                    raise AssessmentError("out_of_scope")
                                page = reader.page(Query(request["surface"], scope.cohort[index]), cursor(request["continuation"]))
                                result = {"rows": page.rows, "continuation": page.continuation}
                            else:
                                raise AssessmentError("out_of_scope")
                            body = canonical({"result": result})
                            if len(body) > MAX_PAGE_BYTES:
                                raise AssessmentError("response_byte_limit")
                            connection.send_bytes(body)
                        except AssessmentError as exc:
                            connection.send_bytes(canonical({"error": exc.code}))
                        except AzureError:
                            connection.send_bytes(canonical({"error": "sdk_read_unavailable"}))
                        except (AttributeError, KeyError, TypeError, ValueError):
                            connection.send_bytes(canonical({"error": "invalid_sdk_metadata"}))
                finally:
                    reader.close()
    except AzureError:
        connection.send_bytes(canonical({"error": "credential_unavailable"}))
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        connection.send_bytes(canonical({"error": "reader_initialization_failed"}))
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class IsolatedReader(AbstractContextManager):
    """One bounded SDK subprocess; a timed-out source cannot discard cohort rows."""

    def __init__(self, scope: Scope):
        self.scope = scope
        self.connection, child = multiprocessing.get_context("spawn").Pipe()
        self.process = multiprocessing.get_context("spawn").Process(target=_serve, args=(child, scope.private()))
        self.child = child
        self.failed = False
        self.started = time.monotonic()

    def __enter__(self) -> IsolatedReader:
        self.process.start()
        self.child.close()
        return self

    def _stop(self) -> None:
        self.failed = True
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1)

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.failed:
            raise AssessmentError("reader_unavailable")
        remaining = COLLECTION_SECONDS - (time.monotonic() - self.started)
        if remaining <= 0:
            self._stop()
            raise AssessmentError("collection_timeout")
        try:
            self.connection.send_bytes(canonical(request))
            if not self.connection.poll(min(SOURCE_SECONDS, remaining)):
                self._stop()
                raise AssessmentError("source_timeout")
            response = obj(strict_json(self.connection.recv_bytes(MAX_PAGE_BYTES)))
        except (EOFError, OSError):
            self._stop()
            raise AssessmentError("reader_unavailable") from None
        if "error" in response:
            raise AssessmentError(response["error"])
        return obj(response.get("result"))

    def metadata(self, source: MetadataSource) -> dict[str, Any]:
        return self._call({"operation": "metadata", "source": source})

    def page(self, query: Query, continuation: str | None) -> Page:
        query.validate(self.scope)
        raw = self._call({
            "operation": "page", "cohortIndex": self.scope.cohort.index(query.pair),
            "surface": query.surface, "continuation": cursor(continuation),
        })
        return Page(raw["rows"], raw["continuation"])

    def __exit__(self, *_args: object) -> None:
        if not self.failed:
            try:
                self.connection.send_bytes(canonical({"operation": "close"}))
            except (OSError, EOFError):
                self.failed = True
            self.process.join(timeout=1)
        self._stop()
        self.connection.close()
