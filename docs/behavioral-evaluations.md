# Behavioral evaluations

AI4IA's development-only evaluator runs versioned synthetic tasks through the real
application and scores their observable behavior. The mandatory **offline**
program does **not** measure live model quality, call Foundry, read production
traces, or export execution receipts. The dormant live authored-task client and
scheduled workflow have been retired; the API's actor restrictions remain.
The program and its datasets live in `scripts/evaluations`, outside the application
Docker contexts.

## Run and compare

Use Python 3.12 and the existing API development dependencies, installed as
described in [the contributor guide](../AGENTS.md#api-appapi-python-312).
Run these commands from the repository root:

```powershell
# No output option: print the content-free JSON report to stdout.
python -m scripts.evaluations run

# Explicit new paths: preserve the baseline rather than overwrite it.
python -m scripts.evaluations run --output baseline.json
python -m scripts.evaluations run --output candidate.json
python -m scripts.evaluations compare baseline.json candidate.json

# Regression controls and the same static gates used by app-ci.
python -m pytest -q scripts/tests/test_behavioral_evaluations.py
ruff check --config app/api/pyproject.toml scripts/evaluations scripts/tests/test_behavioral_evaluations.py
pyright --project scripts/evaluations
```

Prefer paths outside the checkout for local artifacts. The destination's parent
directory must already exist. Report creation is exclusive: an existing file or
symlink is an error, not an invitation to replace a baseline. JSON reports are
bounded to 256 KiB and validated on both write and read.

Exit codes are **0** for a fully passing run, **1** for a deterministic failure,
and **2** for unknown/unscored execution or a refused/invalid operation. Failures
take precedence over unknowns, but both remain visible in the report. Comparison
returns 1 for regressions, 2 for remaining unknown cases, or 0 otherwise.
Configuration/identity failures before execution refuse the run rather than
produce a success-shaped or unidentified report.

The existing `app-ci` API job runs this program on every PR, using its existing
dependencies. It retains the content-free `behavioral-evaluation` artifact for
seven days, including failed runs. No new workflow, cloud resource, credential,
scheduled spend or production deployment is needed.

## What actually executes

The fixtures supply native provider response bodies; they do not supply expected
execution receipts. The evaluator drives `create_app`, FastAPI `TestClient`, real
`ModelGatewayClient` HTTP encoding/decoding, application orchestration and persisted
message reads. HTTPX `MockTransport` replaces the provider, and the existing SSE
fixture helper fragments text and function-call arguments.

| Golden task | Execution seam and deterministic oracle |
| --- | --- |
| Chat and streamed chat | Real chat endpoint, persistence, complete terminal receipt, content and JSON Schema |
| Agents on Chat Completions, Responses and Anthropic Messages | Real calculator, registry/executor, tool-loop replay, required/forbidden calls, computed result and feedback delivered to the next model request |
| Workflow handoff | Real workflow CRUD/run, independently evidenced child calls, previous output in the next step's prompt, parent and step receipts |
| Unattended external-tool workflow | Real MCP capability and approval checks, no fake connector dispatch without invocation consent, visible denied-call receipt |
| Owned document and streamed provenance | In-memory canonical library/blob and derived chunks, equally similar owned/foreign sources, real retrieval and citation attestation; source owner, revision, excerpt digest, prompt span and citation must agree |
| Exact tool approvals | Real server-minted grants and same-turn continuation, one dispatch for two identical emissions, no model reconstruction or extra message, exhausted-grant replay refusal, changed-argument/cross-session/cross-owner refusal |
| Refusal and annotation posture | Synthetic refusal text, forbidden content/tools, real provider safety parsing and annotate-only evidence; unavailable assessment is not relabeled as a clean verdict |

Required tools must succeed, not merely appear in a model response. Forbidden
tools fail even if their attempt was denied. Tool results are checked against the
fixture's task expectation and the subsequent adapted provider request. Citation
checks independently require fixture-owned sources even if a faulty attestation
were to label a fabricated citation "verified".

The controls seed wrong tools, fabricated citations, malformed output, changed
refusal/annotation posture, cost/latency overages and an actual application
approval-gate bypass. Each has the same-fixture passing control. These are
orchestration and deterministic-contract evaluations, not evidence that a real
model would choose the scripted answer or resist a prompt injection.

Dataset, runner and oracle version 1.1.0 use `/api/chat/continue` rather than the
retired approval retry turn. A second identical invocation remains held after
the first is approved; replay cannot execute it. Changed arguments are a new
held turn, whose challenge cannot use the previous turn's grant. Cross-session
and cross-owner cases likewise obtain a legitimate target challenge and refuse
the foreign grant before model or tool dispatch. Same-turn snapshots prove the
stable assistant and operation identities; only the latest cumulative receipt
enters usage/cost totals. New user turns invalidate old authority explicitly.
The four approval cases remain in the denominator with tighter model-call
budgets. This intentional execution/scoring change requires a fresh baseline;
it does not weaken transport isolation or change the content-free report shape.

## Scores, unknowns and bounds

Every dataset case has one result and every result has all twelve check slots.
An inapplicable dimension is `unscored`, not a fabricated pass. A case passes
only when its applicable checks pass; any failed check fails it, and any unknown
applicable check makes it unknown unless another check failed. No applicable
scores means an unscored case and a nonzero gate.

Both per-case and per-check coverage publish total, passed, failed, unknown,
unscored and scored counts. **The pass denominator is always all declared
cases**, not only scored cases. Report validation rejects missing/duplicate rows,
missing check slots and inconsistent summaries. Comparison also binds that
manifest to the selected committed dataset, so a self-consistent filtered report
cannot quietly remove a failure.

Cost comes from the application's receipt/pricing-snapshot machinery using
explicitly synthetic token rates. Missing provider usage or pricing stays
unknown/null, never zero. A partial known subtotal is labeled separately and
cannot satisfy a total-cost ceiling. The price fixture is not a billing estimate.
Latency is the sum of scripted provider delays for requests actually observed,
not workstation wall-clock timing or a live-service SLO.

The suite accepts at most 32 cases, 128 KiB of dataset JSON, 12 replies per case,
16 KiB per reply and 32 KiB per case. Each case has a fresh sequential subprocess
with a 30-second wall-clock deadline; a timeout terminates the worker. Crashes,
missing stdout, malformed outcomes and timeouts become retained unknown rows.
Malformed datasets, duplicate JSON keys, nonfinite values, oversized/deep inputs
and JSON Schema references are rejected. There is no case filter, remote schema
resolver or production dataset ingestion option.

## Versions and intentional updates

The report names the dataset/config/prompt versions, runner and oracle versions,
catalog model versions, provider-wire fixture versions, synthetic pricing version,
application Git revision and source-tree digest. Digests also bind the exact
dataset, effective settings, prompts, catalogs, evaluator/helper source and
Python/dependency environment. Effective instruction hashes come from executed
receipts; they are not hashes of production prompts.

Models are selected deterministically from the authoritative catalog by protocol
and tool capability; no deployment name is typed into the evaluator. Their
versions come from the selected regional/SKU deployment in `infra/models.json`.
**These are configured catalog versions, not live observed provider versions.**
The provider-version basis and `live_quality: not_measured` make that distinction
explicit in the report.

Comparison permits an identified application source revision/tree change.
Dataset, config, prompt, evaluator, provider fixture, selected model/version,
catalog and dependency-environment differences are incompatible by default.
Changed executed instruction hashes also require an intentional rebaseline.
There is no override to compare unidentified or incompatible runs.

To extend the suite, author only synthetic material in
`scripts/evaluations/datasets/synthetic-v1.json`, add paired controls in
`scripts/tests/test_behavioral_evaluations.py`, and update the relevant version
when changing its meaning. Change the prompt version for instruction/input
changes; the config version for posture/rates; provider fixture versions for wire
contracts; oracle/runner versions for scoring/execution semantics. Increment the
report schema generation for incompatible report shapes. Digests detect edits
even if someone forgets a version bump, but do not replace human review of the
version change. Keep old artifacts; never edit an old result to make it compatible.

## Isolation and remaining approval boundaries

Workers inherit only Windows loader essentials where needed, not proxies,
telemetry settings, Azure credentials, app settings, home-directory variables or
`PYTHONPATH`. They use isolated Python startup and explicit checkout imports.
Dotenv sources are disabled before importing the application or test helpers,
including the application's import-time default instance; fixture overrides alone
would arrive too late. The local configuration uses in-memory canonical stores
and disabled exporters.
Real sync/async HTTP transports, DNS and socket connections are denied; the sole
socket exception is stdlib asyncio socketpair construction. Fixtures never
connect to their synthetic gateway or MCP URLs. Lifespan teardown closes the
application and the subprocess releases its synthetic stores.
An explicit OpenTelemetry suppression scope also prevents export when these
fixtures execute inside an already-instrumented test process. Capture controls
exercise a real in-memory exporter and demonstrate that it emits outside that
scope. Offline runner version 1.0.1 records this stronger isolation contract.

Reports use an allowlisted content-free schema: no user/session/correlation ids,
prompts, replies, document excerpts, filenames, tool payloads, approval grants,
raw exceptions or receipt payloads. The runner has no production-trace input or
telemetry export path.

The offline command still refuses `--mode live`, any non-disabled `--judge`, and
`--production-content`. Its worker environment and transport isolation are not
weakened to reuse it for live calls.

## Retired live client and retained identity safeguards

The unused live authored-task driver, live-only dataset, dedicated schedule and
client-specific tests are removed. The offline evaluator above is unchanged.
No replacement live runner, production-trace input, paid judge or schedule is
introduced. Live model quality remains unmeasured by this program.

Client retirement does not erase earlier synthetic owner conversations,
unresolved uploads, reports, accounting or backups, and does not revoke or relax
an identity's policy. Keep configured `canaryActor`, `evaluationActor` and
`realtimeCanaryActor` restrictions. The distinct evaluation profile, request-only
tool/memory reductions, one-shot fresh-session guards and owner-scoped cleanup
remain in the API, including during policy pause or refresh failure.
`app/api/tests/test_policy_execution_profiles.py` exercises signed identities,
real factory/adapter dispatch, tampered reductions, output/prompt bounds,
missing guards and owner cleanup without any live-client imports.
The existing policy-binding, actor and fresh-session suites remain.

Any removal of credentials, registrations, grants or retained data needs its own
explicit scope and approval; see
[the retained cleanup boundary](runbooks/feature-enablement.md#retired-scaffolding-and-operational-add-ons).
Content-free [GenAI telemetry](runbooks/telemetry.md#content-free-genai-model-spans)
still does not grant content collection. Future live evaluation, paid judges or
production traces require separate identity, privacy, spend and retention
decisions and cannot weaken the offline isolation or the
[non-blocking safety assessment policy](rai-decision-record.md).
