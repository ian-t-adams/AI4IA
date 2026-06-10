"""Agent-callable document processing (Phase 11H).

The third leg of the "capability-as-tool" triad (images, video, documents): a
synthetic ``process_document`` tool that runs an LLM analysis/extraction over a
document the authenticated user already has in their library, reusing the
already-extracted parsed content (Content Understanding / ingest) rather than
re-implementing upload or ingestion.

Layout mirrors :mod:`ai4ia_api.images` and :mod:`ai4ia_api.videos`:

* :mod:`service` — the shared, governed core (:class:`DocumentProcessingService`)
  that owns instruction/mode validation, the reuse of the ready-library read, the
  single model-gateway call, and upstream-error sanitization, so the tool (and any
  future HTTP endpoint) can never drift.
* :mod:`artifacts` — durable per-user storage for an over-cap result, reusing the
  document library's blob account (in-memory fallback locally/in tests).
* :mod:`capability` — the closure-bound ``process_document`` capability injected
  into an agent turn, with a per-turn cap, entitlement gate, and the inline-vs-
  artifact result split.

The whole domain is inert unless ``settings.document_understanding_enabled`` is
true (the chat router only injects the capability when the document retrieval
consumer exists), reusing the same flag as the rest of the document library.
"""
