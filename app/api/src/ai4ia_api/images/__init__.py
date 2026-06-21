"""Image generation domain.

The shared generation core (:mod:`service`), durable artifact storage
(:mod:`artifacts`), and the agent-callable ``generate_image`` synthetic
capability (:mod:`capability`). The same governed generation path backs both the
HTTP endpoint (``routers/images.py``) and the tool, so model/size validation and
upstream-error sanitization live in exactly one place.
"""
