"""Video generation domain.

The shared generation core (:mod:`service`), durable artifact storage
(:mod:`artifacts`), the agent-callable ``generate_video`` synthetic capability
(:mod:`capability`), and the single availability predicate every advertising seam
shares (:mod:`availability`). Generated videos are persisted to Blob storage and
served back through the API, never linked directly from the upstream model. The
same governed path backs both the HTTP endpoint (``routers/videos.py``) and the
tool, so model validation and upstream-error sanitization live in exactly one
place.
"""
