# Azure Container Apps Sandboxes evaluation

> **Decision (2026-09-03): evaluate, do not replace production Code
> Interpreter yet.** The shipping path remains FastAPI -> dedicated Code
> Interpreter APIM API -> Foundry Responses/Files. No Sandbox Group, identity,
> role assignment, disk image, volume, or preview resource is provisioned by this
> repository.

Azure Container Apps Sandboxes are a promising future execution substrate for
`run_code` and `analyze_attachment`. They provide stronger executor isolation,
deny-by-default egress, user-controlled images, byte-level file exchange, and
state snapshots. They are not a drop-in replacement for the Azure-managed
Responses Code Interpreter: AI4IA would have to own the code-generation loop,
execution lifecycle, artifact collection, metering, cleanup, and supply chain.

## Verified platform surface

Microsoft documents Sandboxes as a first-class `Microsoft.App/sandboxGroups`
resource with a separate Azure Dev Compute data plane. The current Python package
is `azure-containerapps-sandbox` and exposes asynchronous group and sandbox
clients for creation, command execution, files, egress policy, snapshots,
volumes, suspend/resume, and deletion.

| AI4IA requirement | ACA Sandbox capability | Assessment |
| --- | --- | --- |
| Isolated untrusted code | Per-sandbox secure boundary | Strong fit |
| Raw attachment/document bytes | `write_file` / `read_file` | Better than provider file ids |
| Return generated artifacts | Read output bytes directly | Strong improvement |
| No executor egress | Full inspection with default `Deny` | Strong improvement |
| Custom runtime packages | OCI disk image, including ACR images | Strong improvement; AI4IA owns patching |
| Fast start | Prewarmed service; custom images still need realistic measurement | Validate |
| One synchronous analytical response | Sandbox returns stdout/stderr/exit code, not a model-authored answer | Missing; requires a bounded model/execute loop |
| One metered attempt | One logical run becomes create + files + N execs + reads + delete | Metering contract change |
| 120-second execution timeout | No documented per-`exec` timeout | Must enforce in the harness and command |
| Narrow runtime RBAC | Documented Data Owner role controls the whole group data plane | Too broad today |
| Stable IaC | Preview and stable ARM schemas currently disagree | Blocking |

## Current blockers

1. **ARM contract churn.** Current portal/samples use
   `Microsoft.App/sandboxGroups@2026-02-01-preview` with resource defaults and
   identity. The public stable `2026-07-01` schema exposes a materially different
   property set. Microsoft warns that preview sandboxes may need recreation.
2. **SDK maturity.** The Python SDK is a beta/community-preview package rather
   than a stable Azure SDK release.
3. **Unknown economics and availability.** No authoritative pricing, regional
   availability matrix, or quota table was found.
4. **Coarse data-plane authority.** `Container Apps SandboxGroup Data Owner`
   includes sandboxes, images, snapshots, volumes, and secrets for the group.
5. **No analytical response contract.** ACA executes code; it does not replace
   the model loop that chooses code, interprets output, and writes the answer.
6. **Lifecycle ownership.** Failed deletion can leave a billable sandbox or
   snapshot, so a janitor and durable ownership records are mandatory.

## Proposed default-off PoC

The PoC should preserve the existing `CodeInterpreterResult` boundary so
`run_code` and `analyze_attachment` do not acquire a second behavior contract.

1. Build an AI4IA-owned Python OCI image with pinned analysis packages.
2. Create one sandbox per user/session/turn with opaque hashed labels.
3. Apply full-inspection egress with default `Deny` and no ports.
4. Stage bounded source bytes under `/workspace/input`.
5. Run at most six model-authored cells. Each cell is written to a file and
   executed under both an application timeout and an in-command timeout.
6. Cap stdout/stderr, nonce-fence it as untrusted, and send it back through the
   normal gateway model loop.
7. Read approved artifact bytes from `/workspace/output` into the existing
   authenticated artifact store.
8. Delete the sandbox in `finally`; run a separate janitor over expired AI4IA
   labels.
9. Keep exact-argument approval and one logical compute entitlement charge per
   user request. Add sandbox duration/resource statistics without changing the
   existing unit silently.

`analyze_attachment` is the preferred first experiment: it is already
default-off, benefits directly from avoiding a provider file bucket, and has a
bounded source lifetime.

## Security comparison

| Property | APIM-fronted Responses Code Interpreter | ACA Sandboxes |
| --- | --- | --- |
| Executor maintenance | Microsoft-owned | AI4IA-owned OCI image |
| Executor egress | Opaque to AI4IA | Deny/allow/transform policy plus decisions |
| File handling | Foundry Files API, best-effort delete | Direct sandbox filesystem and volumes |
| Inbound governance | Dedicated APIM API, fixed model/tool/storage posture | Separate Azure Dev Compute API and identity |
| Runtime authority | APIM managed identity to primary Foundry account | Group Data Owner over every sandbox asset |
| Output | Model answer plus logs/artifact references | Process output and artifact bytes; AI4IA must synthesize |
| Stability | GA Responses API | Sandbox platform and SDK preview |

The two designs are complementary: APIM constrains the inbound Foundry call;
Sandboxes would constrain the executor itself. Preview research is not a reason
to remove the APIM boundary.

## GA adoption gates

Do not make ACA Sandboxes the default until all of these are true:

- a stable ARM schema used by official samples is published;
- the Python SDK reaches a stable 1.x release;
- pricing, regions, and quota limits are documented and measured;
- a data-plane role narrower than full group ownership exists, or its breadth is
  explicitly accepted;
- file-size and command-timeout contracts are documented;
- the PoC passes isolation, egress, cleanup, artifact, metering, and failure
  tests against a disposable environment.

## Primary references

- [Azure Container Apps Sandboxes overview](https://learn.microsoft.com/azure/container-apps/sandboxes-overview)
- [Configure sandbox egress policies](https://learn.microsoft.com/azure/container-apps/sandboxes-egress-policies)
- [Azure Container Apps Sandboxes samples](https://github.com/Azure-Samples/azure-container-apps-sandboxes)
- [Azure REST API specifications](https://github.com/Azure/azure-rest-api-specs/tree/main/specification/app/resource-manager/Microsoft.App/ContainerApps)

