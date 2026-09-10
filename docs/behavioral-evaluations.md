# Behavioral evaluations

AI4IA's development-only evaluator runs versioned synthetic tasks through the real
application and scores their observable behavior. The mandatory **offline**
program does **not** measure live model quality, call Foundry, read production
traces, or export execution receipts. A separate, default-off live driver is
described below; it never runs under the offline or pull-request gate.
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
python -m pytest -q scripts/tests/test_behavioral_evaluations.py scripts/tests/test_live_evaluations.py scripts/tests/test_live_evaluation_api.py
ruff check --config app/api/pyproject.toml scripts/evaluations scripts/tests/test_behavioral_evaluations.py scripts/tests/test_live_evaluations.py scripts/tests/test_live_evaluation_api.py
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

## Separate opt-in live authored tasks

`python -m scripts.evaluations.live` is a separate CLI and worker. The committed
`live-synthetic-v1.json` contains exactly three no-tool tasks: a small arithmetic
JSON object, a fixed quoted-task-instruction response, and an unavailable-source
JSON format. The server is asked for `allowTools=false`,
`allowAutomaticMemory=false`, and `requireFreshSession=true` on **every** turn.
These are request reductions, not authority to override identity, entitlements,
model policy, tool approvals, or memory preferences. Dataset/prompt version 1.1.0
encodes each authored task specification and quoted data in **one user message**,
leaving the session's system prompt unset. It does not relax the fresh-session
guard or test system-versus-user role obedience.

The live suite measures only those deterministic authored outcomes. Tool choice,
tool feedback, citation grounding, approvals, broad safety and workflow quality
remain **unscored**, not passing because no tool ran. Their broader deterministic
coverage remains in the offline dataset. Open-ended quality is unmeasured. The
unavailable-source task checks an output format, not a real citation corpus or a
general resistance-to-injection claim.

Live HTTP goes only to the configured governed API origin. The API retains model
routing through SimpleL7Proxy -> APIM -> Foundry. The driver does not load an
application instance, import an installed API checkout, call a model SDK, read
production traces, list users' records, or fetch external datasets. It validates
all DNS answers as public, connects to one exact address with the original TLS
SNI/Host, and accepts no redirects, inherited proxy settings, cookies or
credential discovery.

### Authorization and activation are separate

Source delivery is **not** authorization to invoke the driver or enable its
workflow. A human must approve an API-only dedicated authored-synthetic actor,
its model access, synthetic-data lifecycle, spend exposure and provider-variance
policy. Existing deployment credentials must not be reused. No identity,
federation, grant, resource, deployment setting or data migration is created here.
Lack of an approved identity is an unmet prerequisite, not evidence that no
identity is needed.

The policy-owned `GET /api/execution-capabilities` read must report version 1,
current-owner binding and the distinct `authored-synthetic-evaluation` profile
for the selected model/region. Its `ready` contract includes a known non-admin
evaluation actor, model permission, no tool/automatic-memory/data authority and
the real one-shot fresh-turn dispatch guard. Its profile query is a selector,
never an authorization assertion. The operator's `evaluationActor` marker in
the existing policy configuration adds restrictions, not grants; it is absent
by default. This driver neither installs that marker nor configures its identity.
The monitor's `canaryActor`, fixed sentinel and smaller output envelope must
never be reused or weakened to accommodate authored quality prompts.

The `live-evaluations.yml` workflow is main-only and uses a dedicated OIDC login
with `allow-no-subscriptions`; an Azure subscription role is not requested by
this source. For the existing repository, the main-ref subject is
`repo:ian-t-adams/AI4IA:ref:refs/heads/main`, with audience
`api://AzureADTokenExchange`. Review the actual API app-role/assignment policy
and actor object id separately. Never infer an actor grant from a successful
Azure login. There is no GitHub environment in this workflow, so adding one
changes the federation subject and requires a separate review.

These are dedicated **repository/CLI variables**, not azd deployment variables:

| Variable | Required meaning |
| --- | --- |
| `AI4IA_LIVE_EVAL_ENABLED` | Unset/off by default; exact `true` admits a separately authorized finite run |
| `AI4IA_LIVE_EVAL_SCHEDULE_ENABLED` | Separate exact `true` admits the weekly schedule only after manual evidence; unset/off by default |
| `AI4IA_LIVE_EVAL_API_ORIGIN` | Exact public HTTPS API origin, no credentials, path, query or fragment |
| `AI4IA_LIVE_EVAL_API_AUDIENCE` | Exact API app audience GUID or `api://` GUID |
| `AI4IA_LIVE_EVAL_CLIENT_ID` | Dedicated synthetic application's client id, never the deploy client |
| `AI4IA_LIVE_EVAL_TENANT_ID` | Explicit tenant for that actor and API |
| `AI4IA_LIVE_EVAL_ACTOR_OBJECT_ID` | That actor's service-principal **object** id, not its client id |
| `AI4IA_LIVE_EVAL_MODEL_ID` | Explicit approved model **id** in the source and advertised catalogs, not a deployment name |
| `AI4IA_LIVE_EVAL_LIMITS_ACK` | Exact `finite-requests-not-a-bill-cap`; acknowledgment is not proof of a bill cap |
| `AI4IA_LIVE_EVAL_DEPLOY_CLIENT_ID` | Known deploy client id to reject reuse; the workflow derives this from `AZURE_CLIENT_ID` |
| `AI4IA_LIVE_EVAL_TOKEN` | Local process-only approved actor API token; the workflow acquires it in memory and never uploads it |

The CLI locally binds token claims to the configured tenant, object id, client
and API audience, refuses delegated scope tokens and requires adequate remaining
lifetime. This is a **misconfiguration guard, not JWT signature verification**.
The API independently authenticates and authorizes the actual bearer token.
Token, actor, API-origin and identity hashes are absent from reports.

After approval and with those variables supplied through an approved local
credential flow:

```powershell
# Authorized read-only API policy/catalog/compatibility preflight; no fixtures or model call.
python -m scripts.evaluations.live preflight

# Separate authorization required: one finite run, one new exclusive output path.
python -m scripts.evaluations.live run --output <new-local-report.json>
```

The read-only preflight observes the current authenticated policy capability,
catalog and schema compatibility. A missing/unready/unbound/monitor policy or
unsafe constraint refuses before fixtures. Exit 0 on **preflight** means that
these read-only observations passed, not that a quality case ran: all three
quality rows remain unknown and cleanup remains unproven. The policy read is
never a cached grant; the real server rechecks authority at dispatch.

The deliberately invalid chat-validation probe omits both required fields; even
an older server that ignores the new controls cannot execute it. A 422 field
inventory is only schema compatibility, not execution enforcement. The separate
versioned policy/factory declaration and real-API boundary controls remain
necessary. Preflight cannot certify future provider behavior, fixture cleanup,
spend or live quality, and it creates no session or fresh-turn claim.

The integration controls use real signed JWT validation, the production policy
and fresh-dispatch factories, actual session/receipt/deletion routes and the
real gateway adapter with only its HTTP provider replaced. They exercise the
complete driver, incompatible/expired/privileged actors, changed policy, absent
factory callbacks, altered request reductions, one-shot replay and foreign-owner
refusal. They are still offline fixtures, not live actor or rollout evidence.

### Finite work and cleanup

Each run allows at most **48 API attempts**, including policy, catalog and
compatibility reads, an empty lifecycle control, all session creation/chat/read calls, and
every delete/status/reconcile call. There are no write retries or response
replays, and no permanent background loop. The worker has a 240-second work
budget and a parent deadline of 255 seconds. Each HTTP operation is bounded to
45 seconds. Normal work cannot consume the last eight requests or 45 seconds
reserved for the current fixture's cleanup. The driver checks that create,
chat and persisted-message read fit before starting another task.

Requests are at most 8 KiB, each response at most 96 KiB, aggregate accepted
response bytes at most 1 MiB and reports at most 64 KiB. A one-byte overflow
sentinel detects a bound violation. Worker stdout is bounded while produced;
stderr and exception text are not retained. Worker failure retains all three
unknown case rows and unknown request/byte counters, never a false zero.

Before the first model task, the enabled live run creates **one new empty**
session and proves its current deletion path. This mutating lifecycle control is
recorded separately from quality-case counts. It cannot certify future task
cleanup. Every task session must independently reach exact-id
`cleanup_verified`, with the expected scope, verified child stores, no unresolved
upload intents and a valid verification timestamp. Up to three owner-resumed
reconciles are included in the same budget. Legacy 204 deletion, a 202
acknowledgment, a 404, or an empty list is **not** that proof.

This requires separately approved activation of the existing
[conversation deletion protocol](runbooks/conversation-deletion.md). The driver
does not enable it, enroll old records or expose private protocol fields.
Ambiguous creation with no usable id remains incomplete; it is never replayed or
recovered by an owner/global orphan scan. A cleanup failure stops later tasks.
The proof covers conversation content and inline originals only; retained
coordination, usage accounting and backups are not physically erased.

Only priced, sampling-capable Chat Completions models with an advertised output
limit are admitted by this first live profile. Requests use at most 256 output
tokens. Responses' minimum-output floor, reasoning-only paths, media, unpriced
models and unsupported caps refuse before inference. The executed receipt must
show one model call, one HTTP attempt and the actual capped request parameter.
Missing or changed receipt pricing/usage remains unknown and stops further work.

**These are application/request/observation bounds, not a proven Azure bill
cap.** The proxy/provider's internal attempts and billable processing after a
timeout cannot be inferred from the client's request count. Flat token-price
snapshots do not price every cache, geography, priority or non-model meter.
No retry-pool multiplier, provider-internal count, quota cutover or billing
guarantee is invented. A recorded cost ceiling is a deterministic observation
check, not distributed hard admission.

### Reports, variance and remaining decisions

Reports keep every declared case and every check slot. They contain only
allowlisted statuses/counts, latency, observed token-cost metadata, digests and
source/catalog versions. Prompts, replies, tool payloads, source excerpts,
session/user ids, raw responses/events/exceptions and credentials are never
serialized. Authored prompts remain in the committed source dataset; live
responses exist only in worker memory and the new API-owned fixture until its
scoped cleanup completes.

The driver source revision/tree, dataset/prompt/evaluator versions, source
catalog/prices, dependency environment, observed policy/control contract versions
and advertised catalog projection are
identified. Source and advertised model versions are **configuration**, not
observed provider versions or proof of the deployed application revision. Those
unobserved fields stay null. Automatic live report comparison is deliberately
unsupported; no silent cross-target/model-version rebaseline is possible.

For **run**, exit 0 requires all applicable checks and every cleanup proof. A complete
deterministic failure returns 1; any incomplete/unknown run returns 2, including
provider outage or ambiguous cleanup even when another check failed. The
independent workflow retains only the exact content-free report for seven days
and fails visibly for unknown/unavailable execution. It is not a required PR
check, and provider variance never silently becomes one. Canceling a live worker
can prevent cleanup; cancellation is not evidence of no data or no charge.

Optional judges remain disabled: all current declared dimensions have
deterministic oracles. This does **not** claim that open-ended task quality can
be reduced to them. A future judge requires a justified dimension, approved
model/version, calibration, uncertainty and spend policy before implementation.
Both CLIs continue to refuse production-content ingestion and non-disabled
judges. Production trace evaluation requires a separate explicit
`AppGenAIContent` protection, RBAC, privacy/consent, retention and deletion
decision before any payload collection or emission.

Content-free [GenAI telemetry](runbooks/telemetry.md#content-free-genai-model-spans)
uses the existing telemetry exporter; it does not grant content collection.
Neither offline results nor live source/fake-server controls authorize
activation or alter the recorded
[non-blocking safety assessment policy](rai-decision-record.md).
