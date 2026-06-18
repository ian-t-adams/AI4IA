"""Native Microsoft **Web IQ** search capability (default-OFF).

Exposes Web IQ web / news / videos / images / browse search as synthetic tools
(``extra_tools`` / ``extra_handlers``) injected into
:func:`~ai4ia_api.agents.runtime.run_agent_turn`, so any tool-enabled agent — and
the main chat — can fetch current/real-time web information and cite URLs. Mirrors
the inline-attachment analysis and library compute capabilities: a factory builds a
per-turn bundle bound to the authenticated identity + turn nonce and returns
``None`` when the feature flag is off (zero-regression posture).

Built on the official ``webiq`` SDK's async client; the SDK is imported lazily so
the app boots and the tests run without it unless the feature is enabled.
"""
