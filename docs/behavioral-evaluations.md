# Offline behavioral evaluations

AI4IA's development-only evaluator runs versioned synthetic tasks through the real
application and scores their observable behavior. It does **not** measure live
model quality, call Foundry, read production traces, or export execution receipts.
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
| Exact tool approvals | Real server-minted grants, held first call, one dispatch for two identical emissions, exhausted-grant replay refusal, changed-argument/cross-session/cross-owner refusal |
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

Reports use an allowlisted content-free schema: no user/session/correlation ids,
prompts, replies, document excerpts, filenames, tool payloads, approval grants,
raw exceptions or receipt payloads. The runner has no production-trace input or
telemetry export path.

`--mode live`, any non-disabled `--judge`, and `--production-content` explicitly
refuse before execution. They are not latent enablement switches. Still pending
separate approval and implementation under issue #418:

- Live/scheduled evaluation: authorized dedicated synthetic actor, governed API
  through SimpleL7Proxy/APIM, cleanup, cost limits and provider-variance policy;
  it must not silently become a PR-blocking stochastic gate.
- Optional paid judges: justified dimensions, approved judge/version, calibrated
  uncertainty and spend policy; no unapproved model is selected here.
- Content-free OpenTelemetry GenAI integration: verify current official semantic
  conventions and the actual exporter without changing the telemetry dependency
  compatibility pair or introducing content attributes.
- Production-content evaluation: an explicit `AppGenAIContent` protection,
  RBAC, consent/privacy, retention and deletion decision before collecting or
  emitting any prompt, response, tool payload or identity.

Offline results do not approve any of these surfaces or alter the recorded
[non-blocking safety assessment policy](rai-decision-record.md).
