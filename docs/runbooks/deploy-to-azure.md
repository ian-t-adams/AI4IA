# Deploy AI4IA to Azure

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](#guided-deployment)

This is a guided deployment rather than a raw ARM-template launch. AI4IA needs
subscription and model quota preflight, Entra app registrations, GitHub OIDC,
container image promotion, postprovision data-plane reconciliation, and
post-deploy verification. A normal Azure portal **Deploy to Azure** button can
submit an ARM template, but it cannot perform those steps; presenting one as a
complete deployment would leave placeholder Container Apps or fail late after
paid resources were created.

Microsoft documents the ARM-only button contract at
[Create a Deploy to Azure button](https://learn.microsoft.com/azure/azure-resource-manager/templates/deploy-to-azure-button).
This repository deliberately links the familiar button to the complete workflow
instead.

## Guided deployment

1. Fork the repository so the deployment workflow and its protected environment
   belong to you.
2. Complete the tool, subscription, quota, identity, and cost prerequisites in
   [Greenfield Azure standup](./greenfield-standup.md).
3. Keep `AI4IA_MODEL_CAPACITY_PROFILE=baseline` for the first deployment. The
   portable baseline is designed to fit more subscriptions than this repository's
   maximum profile.
4. Configure the required repository variables and environment secrets, then run
   **Actions -> deploy -> Run workflow** with **provision** enabled.
5. Complete the first-release data-plane and custom-domain phases in the
   greenfield guide, then verify the exact image digests and authenticated model
   canary.

## API image dependency lock

Both PR and release builds consume `app/api/uv.lock` for runtime dependencies.
The API Dockerfile first checks it against `pyproject.toml` offline, then installs
the frozen runtime without dev or Foundry provisioning extras. A missing lock
fails the build context copy; a stale lock stops the build before installation.
Neither failure authorizes a range-resolving fallback or an automatic lock update.

Refresh a stale lock in a reviewed dependency change using public PyPI, following
[the contributor guide](../../AGENTS.md#frozen-api-runtime-dependencies).
Keep the Dockerfile's build-only uv pin aligned with `app-ci.yml`'s `UV_VERSION`.
The Python 3.12 tag and OCI index digest are independent pins; a dependency-lock
failure is not a reason to refresh them.

Frozen runtime dependencies do not establish byte-for-byte image reproducibility
or signed provenance. Isolated package build tooling is not in the runtime lock;
production SBOM/signing, exact-subject verification before deployment, and
read-only base-index drift reporting remain separate work.

## Use all available model capacity

After the baseline deployment exists, generate a maximum profile for that
subscription:

```powershell
python scripts/sync-model-capacity.py `
  --subscription <subscription-guid> `
  --resource-group <resource-group> `
  --environment-name <azd-environment> `
  --output-plan .azure/model-capacity-plan.json

# Review the plan, then record it in IaC.
python scripts/sync-model-capacity.py `
  --subscription <subscription-guid> `
  --resource-group <resource-group> `
  --environment-name <azd-environment> `
  --apply
```

The script reads only existing deployments and infers whether each pool is
global, data-zone, or regional from Azure's quota and `modelCapacities`
responses. It never reduces a live deployment and refuses missing or ambiguous
pool data.

Commit the resulting `infra/models.json`, set the repository variable
`AI4IA_MODEL_CAPACITY_PROFILE=maximum`, and run the deploy workflow with
**provision** enabled. Maximum capacity is subscription-specific and can consume
all quota for those model pools, leaving no headroom for another application or
concurrent deployment. Standard token-per-minute capacity is still billed by
actual model usage; this profile does not create Provisioned Throughput Units.
