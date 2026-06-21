"""Per-user document library.

A user's durable, cross-session library of uploaded documents and the analyzers
used to crack them. Distinct from the per-session attachments
(``sessions/`` + ``routers/documents.py``): those are scoped to one chat and
hold inline extracted text; the library is partitioned by ``/userId`` and is the
foundation the later sub-phases (Content Understanding ingest, chunking, RAG)
build on.

11A is the storage spine only — models, ownership/dedupe helpers, the repository
(in-memory + Cosmos), and a feature-flagged CRUD API. It performs no model calls
and is inert unless ``settings.document_understanding_enabled`` is true.
"""
