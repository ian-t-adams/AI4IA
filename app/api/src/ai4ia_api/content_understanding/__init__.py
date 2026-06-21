"""Azure AI Content Understanding (CU) integration.

CU is its own async REST surface (``POST …:analyzeBinary`` → ``Operation-Location``
→ ``GET`` poll), not an OpenAI deployment. This package holds a thin, governed
client and its result model; the ingest orchestrator (``library.ingest``) drives
it. Imported only when document understanding is enabled, so the app and tests
run without any CU configuration.
"""
