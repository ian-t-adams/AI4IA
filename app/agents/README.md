# app/agents — in-process agents library

Agent definitions (persona, model binding via the gateway, allowlisted tool bindings) executed
by the in-house **gateway-native tool-calling loop** + `ToolExecutor`. For v1 this runtime runs
**in-process** inside `app/api`; it can be promoted to its own Container App later (see
`azure.yaml`).

## Contents (phased)
- **Phase 4:** agent definitions (persona, model binding via gateway, tool bindings), workflow
  graphs (sequential/parallel/handoff), and the custom-tool interface.
- Tools are registered through the API's **tool-safety registry** — allowlist, scopes, per-tool
  secrets, egress limits, and human approval for destructive/external actions.

## Notes
- Agents bind to models by catalog id; the gateway resolves region/SKU + applies governance.
- Foundry Agent Service tools and BYO MCP tools are both supported (they are complementary).
- **Future option:** a Microsoft Agent Framework MCP client (or Foundry toolbox) could replace the
  in-house `ToolExecutor` behind the same loop; not implemented today.
