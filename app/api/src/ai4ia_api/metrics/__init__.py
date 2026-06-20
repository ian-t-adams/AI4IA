"""Admin resource metrics (WS4 Part B): Azure Monitor platform metrics.

Read-only, best-effort panels for the admin dashboard (AI Search, Postgres,
Cosmos, Container Apps). Everything degrades to ``unavailable`` rather than
erroring, so the dashboard ships before the diagnostics/resource-ids are wired.
"""
