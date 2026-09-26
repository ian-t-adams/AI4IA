"""Custom photo avatars generated from a text description (Phase 1, default-off).

Create, status, preview, list, delete and report for avatars a user owns. Every
provider call (create, status, delete, the Limited Access capability probe and
the avatar project) goes FastAPI -> SimpleL7Proxy -> the exact-operation APIM
API -> the catalog home account. The one direct egress is the bounded, one-time
fetch of the provider-issued preview link into AI4IA Blob (:mod:`preview`).

Module map:

* :mod:`models` -- the HTTP contract, persisted records and the error type.
* :mod:`catalog` -- the typed ``photoAvatars`` block of the voice catalog.
* :mod:`provider` -- the only module that knows the undocumented provider contract.
* :mod:`preview` -- the preview fetch and owner-scoped Blob artifacts.
* :mod:`store` -- owner-partition records and the per-user limit ledger.
* :mod:`availability` -- the capability probe and the one availability predicate.
* :mod:`service` -- orchestration used by ``routers/photo_avatars.py``.

See ``docs/photo-avatars.md`` for the design and the enablement prerequisites.
"""
