"""User-defined workflows.

Workflows are saved, ordered pipelines of agent steps that a user composes once and
re-runs on demand. They are persisted per user in their own Cosmos container and are
distinct from agents: an agent is a single governed persona invoked on the chat hot
path, whereas a workflow chains several such steps through a separate invocation
surface. Reads are owner-scoped and fail closed, so a disabled or unknown workflow
yields nothing rather than executing.
"""
