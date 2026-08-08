# Runbook: Rotate the proxy-ingress key

The credential the FastAPI API presents to SimpleL7Proxy (`S7P-KEY`). Rotate it
when it may have been disclosed, or on whatever schedule you adopt.

This procedure is **zero-downtime** and was executed end-to-end on
2026-08-04; every command below is one that was actually run.

## Why it is not just "regenerate the key"

Three places hold this value, and they are only ever equal by construction:

| Where | What holds it |
| --- | --- |
| APIM | subscription `ai4ia-api-proxy-ingress`, `primaryKey` |
| Proxy | Container App secret `api-proxy-inbound-key` -> env `ValidateAuthKey1` |
| API | Container App secret `model-gateway-api-key` -> sent as `S7P-KEY` |

Bicep reads the APIM key with `listSecrets()` at **deploy time** and writes it
into both Container App secrets. Neither container queries APIM at runtime.

Two consequences, both of which this procedure depends on:

1. **Regenerating the APIM key alone changes nothing.** Both containers keep
   serving with their static copies, and they still match each other. There is
   no outage, and no urgency between steps.
2. **The switchover is the risky part, not the regeneration.** Whichever side you
   update first disagrees with the other until the second catches up. The proxy
   accepts `ValidateAuthKey1` **or** `ValidateAuthKey2`
   (`server.cs::ValidateAuthKey`), so staging the new key into `Key2` removes
   that window entirely.

## Prerequisites

```powershell
az login
$sub  = 'e852113b-6cb5-441c-ac68-26cff884e479'   # or: azd env get-value AZURE_SUBSCRIPTION_ID
$rg   = 'rg-ai4ia-slurmfactory'                   # or: azd env get-value AZURE_RESOURCE_GROUP
$apim = (az apim list -g $rg --query '[0].name' -o tsv)
$sid  = 'ai4ia-api-proxy-ingress'
```

`az apim subscription` does **not** exist in current Azure CLI — use `az rest`
against the ARM endpoint, as below.

## 0. Establish the oracle

Every step is verified against the live proxy. Set this up first, because it is
also how you prove the rotation worked:

```powershell
$url = 'https://genaiproxy.nomad-analytics.com/openai/status'
function Probe([string]$key) {
  (Invoke-WebRequest -UseBasicParsing -SkipHttpErrorCheck -Uri $url `
     -Headers @{ 'S7P-KEY' = $key } -TimeoutSec 45).StatusCode
}
```

Correct key -> `200`. Wrong or absent key -> `403`.

Capture the current key so you can prove it stops working at the end:

```powershell
$old = (az rest --method post --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.ApiManagement/service/$apim/subscriptions/$sid/listSecrets?api-version=2024-05-01" --query primaryKey -o tsv)
Probe $old      # expect 200
```

## 1. Mint the replacement

Generate the key yourself rather than calling `regeneratePrimaryKey`, so the
charset is known rather than assumed:

```powershell
$bytes = [byte[]]::new(16)
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$new = -join ($bytes | ForEach-Object { $_.ToString('x2') })   # 128-bit, lowercase hex
```

This matches the shape APIM generated originally (32 lowercase hex characters).

> Rotate the **secondary** key at the same time. It is an equally valid
> credential on the same subscription, so leaving it alone leaves a live key
> behind.

```powershell
$bytes2 = [byte[]]::new(16)
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes2)
$newSecondary = -join ($bytes2 | ForEach-Object { $_.ToString('x2') })

$body = @{ properties = @{ primaryKey = $new; secondaryKey = $newSecondary } } | ConvertTo-Json -Compress
Set-Content "$env:TEMP\rot.json" $body -NoNewline
az rest --method patch `
  --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.ApiManagement/service/$apim/subscriptions/$sid?api-version=2024-05-01" `
  --headers "Content-Type=application/json" "If-Match=*" --body "@$env:TEMP\rot.json"
Remove-Item "$env:TEMP\rot.json"
```

**Verify nothing moved:**

```powershell
Probe $old      # still 200 - containers hold static copies
Probe $new      # 403 - not rolled out yet
```

If `$old` is not 200 here, stop: something else changed.

## 2. Stage the new key on the proxy (dual accept)

```powershell
az containerapp secret set -g $rg -n ca-proxy-slurmfactory `
  --secrets "api-proxy-inbound-key-next=$new"
az containerapp update -g $rg -n ca-proxy-slurmfactory `
  --set-env-vars "ValidateAuthKey2=secretref:api-proxy-inbound-key-next"
```

Wait for the new revision to take traffic (~45s), then **verify both work**:

```powershell
Probe $old      # 200
Probe $new      # 200   <- the window is now closed
Probe 'wrong'   # 403
```

Do not continue until `$new` returns 200.

## 3. Move the API to the new key

```powershell
az containerapp secret set -g $rg -n ca-api-slurmfactory `
  --secrets "model-gateway-api-key=$new"
$rev = (az containerapp show -g $rg -n ca-api-slurmfactory --query properties.latestRevisionName -o tsv)
az containerapp revision restart -g $rg -n ca-api-slurmfactory --revision $rev
```

A secret change alone does **not** restart the app — the CLI warns about this,
and the container only reads `secretref` env values at start. The restart is
required.

## 4. Collapse to the new key

```powershell
az containerapp secret set -g $rg -n ca-proxy-slurmfactory `
  --secrets "api-proxy-inbound-key=$new"
az containerapp update -g $rg -n ca-proxy-slurmfactory --remove-env-vars ValidateAuthKey2
az containerapp secret remove -g $rg -n ca-proxy-slurmfactory `
  --secret-names api-proxy-inbound-key-next
```

Remove the env var **before** the secret it references.

**Final verification — both assertions matter:**

```powershell
Probe $old      # 403  <- the old credential is genuinely dead
Probe $new      # 200  <- the new one serves
```

## 5. Confirm the state converges with IaC

No Bicep change is needed. `gateway.bicep` reads
`sharedProxyIngressSubscription.listSecrets().primaryKey`, which is now the new
key, and writes it to `ValidateAuthKey1` — the same end state. The temporary
`ValidateAuthKey2` is absent from Bicep, so the next `azd provision` also drops
it if step 4 was skipped.

## Where the key can leak

The proxy serialises every resolved config value into one startup
"Configuration loaded" event. `Profiles:Auth:Key1` did not match its redaction
list, so the key was written in clear. That is fixed
(`ConfigOptionAttribute.Secret`, covered by `ConfigRedactionTests`) — but the
fix only protects revisions running an image built after it.

**Rotate *after* deploying that fix, not before**, or the new key is written to
the same place the old one was.

Measured on 2026-08-04, this deployment's exposure was narrower than it looks:
`EVENT_LOGGERS` is unset, so the event goes to the default **file** client
(`eventslog.json`) inside the container, which is ephemeral and dies with the
revision. A workspace-table search found no occurrence in API telemetry, and a
separate `ContainerAppConsoleLogs_CL` search covered the proxy. **Do not use**
`az monitor app-insights query` with the classic `traces`/`customEvents` names
here: this is a workspace-based component, and that command returns an empty
result set rather than an error — a false all-clear documented in
[`telemetry.md`](./telemetry.md). Pair every absence query with a row-count
control over the same window.

```powershell
$workspace = '<Log Analytics workspace customerId>'

# Non-vacuity: this must return rows for the chosen window before an absence
# result below means anything.
az monitor log-analytics query -w $workspace --analytics-query `
  "union AppTraces, AppEvents, AppExceptions | where TimeGenerated > ago(24h) | count"

az monitor log-analytics query -w $workspace --analytics-query `
  "union AppTraces, AppEvents, AppExceptions | where TimeGenerated > ago(24h) | where tostring(Properties) contains '$new' or Message contains '$new' | count"

# Proxy console logs are a separate source; the proxy has no App Insights logger.
az monitor log-analytics query -w $workspace --analytics-query `
  "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(24h) | where ContainerAppName_s == 'ca-proxy-<env>' | where Log_s contains '$new' | count"
```

## Related

- Same shape, different credentials: the **model** hop key
  (`proxy-apim-subscription-key`), the **realtime** relay key, and the
  **official MCP** key each have their own APIM subscription. Rotating those
  follows steps 1/3/4; only the ingress key has a `Key2` slot for dual accept.
- [`teardown.md`](./teardown.md) — data recovery posture before destructive work.
