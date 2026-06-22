# Configuration Reference

AI4IA has too many knobs to leave them scattered across Bicep, azd, Container
App env, and README prose. This page is the operator map: set the azd value,
watch the Bicep parameter, and know which runtime setting appears in the app.

## Required deployment ownership values

| Purpose | azd / CI variable | Bicep parameter | Notes |
| --- | --- | --- | --- |
| Accountable owner tag | `AI4IA_OWNER` | `owner` | Do not leave this as a personal fork default. It lands on resource tags. |
| Cost center tag | `AI4IA_COST_CENTER` | `costCenter` | Defaults to `genai-demo`; override for real chargeback. |
| APIM publisher email | `AI4IA_APIM_PUBLISHER_EMAIL` | `apimPublisherEmail` | Required by API Management. Use an operator-owned mailbox, not a person. |
| Budget alert recipients | Bicep override only today | `budgetAlertEmails` | Empty means budget tracking without email notifications. |

## Feature flags and prerequisites

| Feature | azd / CI variable | Bicep parameter | Runtime setting emitted | Required companion config |
| --- | --- | --- | --- | --- |
| Voice Live | `AI4IA_REALTIME_ENABLED` | `voiceLiveEnabled` | `AI4IA_REALTIME_ENABLED`, `VOICE_LIVE_ENABLED`, `API_PUBLIC_URL` | `realtimeAllowedOrigins` outside local/dev. |
| Voice Live tools | `AI4IA_REALTIME_TOOLS_ENABLED` | `voiceLiveToolsEnabled` | `AI4IA_REALTIME_TOOLS_ENABLED`, `VOICE_LIVE_TOOLS_ENABLED` | Requires Voice Live. |
| Document library / Content Understanding | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED` | `documentUnderstandingEnabled` | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED`, `DOCUMENT_LIBRARY_ENABLED` | Cosmos + blob storage; CU endpoint defaults to the primary Foundry endpoint unless overridden. |
| Library compute / export | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | `documentComputeEnabled` | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | Requires document understanding. Code Interpreter endpoint/model default to primary Foundry + `gpt-4.1-mini-*` unless overridden. |
| Inline attachment compute | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | `inlineDocumentComputeEnabled` | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | Uses the same Code Interpreter endpoint/model as library compute. |
| Azure AI Search | `AI4IA_SEARCH_LOCATION` | `searchEnabled`, `searchLocation` | `AI4IA_SEARCH_ENDPOINT` when provisioned | Region must have Search SKU capacity. |
| Image generation | checked-in parameter | `imageGenerationEnabled` | `AI4IA_IMAGE_BLOB_ACCOUNT_URL` when provisioned | Image-capable model deployment and generated-media storage. |
| Video generation | checked-in parameter | `videoGenerationEnabled` | `AI4IA_VIDEO_BLOB_ACCOUNT_URL` when provisioned | Video-capable model deployment and generated-media storage. |
| Custom MCP tools | checked-in parameter | `customToolsEnabled` | `AI4IA_CUSTOM_TOOLS_ENABLED`, `CUSTOM_TOOLS_ENABLED` | Cosmos + Key Vault; Entra auth outside local/dev. |
| Web IQ search tools | `AI4IA_WEBIQ_API_KEY` | `webSearchEnabled`, `webIqApiKey` | `AI4IA_WEB_SEARCH_ENABLED`, `AI4IA_WEBIQ_API_KEY` | API key or managed identity support. |
| Memory / semantic recall | `AI4IA_POSTGRES_LOCATION` | `postgresLocation` | `AI4IA_MEMORY_STORE=mem0` when Postgres is provisioned | Region must allow PostgreSQL Flexible Server for the subscription. |

## Custom domains

| Host | azd / CI variable | Bicep parameter | Failure mode |
| --- | --- | --- | --- |
| Web app vanity host | `AI4IA_WEB_CUSTOM_DOMAIN` | `webCustomDomain` | Empty value removes the custom domain binding on provision. |
| Existing web managed certificate | `AI4IA_WEB_MANAGED_CERT_NAME` | `webManagedCertName` | Empty value derives a cert name and can create/adopt a different cert. |
| Proxy vanity host | `AI4IA_PROXY_CUSTOM_DOMAIN` | `proxyCustomDomain` | Empty value removes the custom domain binding on provision. |
| Existing proxy managed certificate | `AI4IA_PROXY_MANAGED_CERT_NAME` | `proxyManagedCertName` | Empty value derives a cert name and can create/adopt a different cert. |

If a vanity hostname matters, set the domain variables before running
`azd provision`. Pretending this is optional is how you get a green deployment
and a dead vanity URL.

## Validation

CI runs `scripts/validate-feature-prereqs.py` from `infra-validate.yml`. It
checks cross-parameter contradictions that Bicep syntax validation does not
catch: prod with dev auth, Entra without tenant/audience/client ID, Voice Live
tools without Voice Live, document compute without document understanding,
broken custom-domain/cert combinations, and personal ownership defaults.
