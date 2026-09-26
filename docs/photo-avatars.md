# Custom photo avatars: design and phased plan

> **Status (2026-09-25): planned, not implemented.** AI4IA has no avatar code,
> catalog entry, APIM operation, role assignment, flag or storage. This page
> records verified platform behavior and the phased plan. Nothing here is enabled.
> Activation waits on three things: the Limited Access approval for custom text to
> speech avatar, re-approval under [RAI review trigger 3](rai-decision-record.md#review-triggers),
> and the [owner decisions](#owner-decisions) below.

## Requirement and scope

AI4IA must be able to generate custom photo avatars and let users interact with
them.

In scope:

- **Create from a text description.** This is Foundry's "Create with AI" path: a
  prompt plus optional age, gender, ethnicity and style. The service generates a
  portrait and builds a photo avatar from it. No consent recording is involved.
- **Status, preview, list and delete** for the avatars a user owns.
- **Real-time conversation** with an owned avatar through the Speech Voice Live
  provider.
- **Optional: rendered talking-head videos** through batch avatar synthesis.

Out of scope:

- **Avatars from a real person's photo.** That path needs a consent video from the
  person, which AI4IA will not collect.
- **Other custom voice and avatar products.** Custom video avatars, voice sync for
  avatar, and pairing with Custom Neural Voice or Personal Voice each need their
  own Limited Access and RAI decision.
- **Non-human characters.** The service supports only faces that look like a real
  or virtual human.

## Verified behavior (2026-09-25)

This was verified end to end in code against test resources, using Entra tokens
with local auth disabled. Resource identifiers are deliberately left out.

| Surface | Observed |
| --- | --- |
| Create from description | Four avatars reached `Succeeded` in about 30-45 seconds each. Each produced a 1024×1024 PNG at `promptImageUri`, a SAS link that expires in about 12 hours. |
| Batch talking-head video | About 20 seconds per job for a roughly 10-second clip: 512×512, h264 + AAC, 25 fps, with lip-sync and head movement. |
| Voice Live session | `session.update` with a custom photo avatar returned `session.updated` with WebRTC ICE servers and `output_protocol: webrtc`. An unknown avatar name was refused with `avatar_verification_failed`. The WebRTC media stream itself is **not tested yet**. |
| Non-human prompt | A cartoon dog was accepted and animated, but the animation model humanized it. Stylized 3D humans render well; realistic humans look best. |

Findings that shape the design:

- **`customized: true` is required.** Batch synthesis needs
  `avatarConfig.customized: true` for a custom photo avatar. Without it the call
  returns 400 "The specified custom avatar model cannot be found." The public
  custom photo avatar example leaves it out.
- **Undocumented creation API.** The creation REST surface is the Foundry portal's
  own endpoint. It is not in the public REST reference or in azure-rest-api-specs,
  so it can change without notice. Learn documents only portal creation.
- **One owning resource.** An avatar belongs to one Foundry resource. Voice Live
  sessions and batch jobs must target the resource that owns it.
- **No built-in voice.** A photo avatar has no voice of its own; any Azure text to
  speech voice can be paired with it.
- **Out-of-date consent note.** The Voice Live how-to still says a custom photo
  avatar needs a photo plus about a minute of consent audio. That is out of date
  for the Create with AI path.

## Provider contract

Every call goes to the owning account's custom subdomain,
`https://{account}.cognitiveservices.azure.com`, with a token for the
`https://cognitiveservices.azure.com` audience.

| Operation | Request |
| --- | --- |
| Avatar project, once per Foundry project | `PUT /CustomAvatar/projects/{project}_PhotoAvatar?api-version=2023-12-01-preview` with `{"kind":"PhotoAvatar","foundryProjectName":"{project}"}` |
| Create | `PUT /CustomAvatar/projects/{project}_PhotoAvatar/photoavatars/{avatarId}?api-version=2023-12-01-preview` |
| Status | `GET` on the create URL until `state` is `Succeeded` |
| List | `GET` on `.../photoavatars` |
| Delete | `DELETE` on `.../photoavatars/{avatarId}` |
| Rendered video | `PUT /avatar/batchsyntheses/{jobId}?api-version=2024-08-01`, then `GET` until `Succeeded`, then download `outputs.result` |

Create request body. Only `prompt` is required:

```json
{
  "description": "optional",
  "properties": {
    "prompt": "A friendly professional virtual host, head and shoulders, facing the camera directly, plain background.",
    "gender": "Female",
    "age": "YoungAdult",
    "ethnicity": "SouthAsian",
    "style": "Realistic"
  }
}
```

Observed allowed values:

- **gender:** Male or Female.
- **age:** YoungAdult, MiddleAged or Senior.
- **ethnicity:** Asian, White, BlackAndAfricanAmerican, SouthAsian, MiddleEastern
  or HispanicAndLatinx.
- **style:** Realistic, DigitalIllustration or Stylized3D.

The avatar id must match `^[A-Za-z][\w.-]{1,62}[\dA-Za-z]$`.

Rendered video request body:

```json
{
  "inputKind": "PlainText",
  "synthesisConfig": { "voice": "en-US-AvaMultilingualNeural" },
  "inputs": [{ "content": "Hello!" }],
  "avatarConfig": {
    "talkingAvatarCharacter": "{avatarId}",
    "photoAvatarBaseModel": "vasa-1",
    "customized": true,
    "videoFormat": "mp4",
    "videoCodec": "h264",
    "subtitleType": "soft_embedded",
    "backgroundColor": "#FFFFFFFF"
  },
  "properties": { "timeToLiveInHours": 168 }
}
```

Notes on rendered video:

- **Codec.** The default codec is hevc, so request h264 for browser playback.
- **Duration.** `properties.DurationInMilliseconds` reports the rendered length,
  and the time to live can be at most 744 hours.
- **Customer storage.** `properties.destinationContainerUrl` can write results to
  customer storage, but it needs a SAS to an account that allows access from all
  networks. Network-restricted accounts aren't supported.

Voice Live session update, on the existing `/voice-live/realtime` surface:

```json
{
  "type": "session.update",
  "session": {
    "modalities": ["text", "audio"],
    "voice": { "name": "en-US-AvaMultilingualNeural", "type": "azure-standard" },
    "avatar": { "type": "photo-avatar", "model": "vasa-1", "character": "{avatarId}", "customized": true }
  }
}
```

The WebRTC handshake then runs:

1. The client takes the ICE servers from `session.updated`.
2. It creates a browser peer connection and sends `session.avatar.connect` with
   `client_sdp`.
3. The service replies with `session.avatar.connecting`, and the client applies
   its `server_sdp`.

The avatar component (`type`, `model`, `character`, `customized`, `ice_servers`) is
documented in the `2026-04-10` reference, which AI4IA pins today, and in
`2026-07-15`. The lab test used `2026-07-15`.

**Regions.** Custom photo avatar creation, real-time avatar and batch avatar are
all available in:

- westus2, eastus, eastus2 and southcentralus
- southeastasia and centralindia
- westeurope, swedencentral, northeurope and italynorth
- francecentral, with limited capacity

Both eastus2 and swedencentral, AI4IA's primary catalog regions, support all
three.

**Cost.** These are the list prices observed on 2026-09-25 in westus2:

| Item | List price |
| --- | --- |
| Photo avatar creation | $2 per avatar |
| Custom avatar, rendered video (batch) | $2.00 per minute |
| Custom avatar, real-time | $0.60 per minute |
| HD custom avatar, rendered video (batch) | $2.70 per minute |
| HD custom avatar, real-time | $0.80 per minute |

Photo avatars need no deployment or hosting fee. Real-time avatar minutes are
billed on top of the Voice Live session itself. Implementation must source these
prices in `app/api/src/ai4ia_api/data/pricing.json` rather than copy them from
here.

## Access and responsible AI

- **Limited Access.** Custom text to speech avatar, which includes custom photo
  avatars, is available by registration only, for approved use cases. AI4IA's
  registration is still pending; its Custom Neural Voice registration is approved.
  Activation outside local development waits for that approval. A successful API
  call is not entitlement: the runtime capability check must see the approved
  capability, and it fails closed.
- **Terms obligations.** Under the Limited Access terms, a deployment must:
  - use each avatar only for the approved use cases;
  - never use it for uses the Code of Conduct prohibits;
  - disclose the synthetic nature of the service to users;
  - support a feedback channel so users can report issues to Microsoft.
- **Disclosure.** Microsoft's disclosure design guidelines classify a photograph
  or computer-generated rendering of a human as a "human-like persona". That calls
  for high disclosure. Label every preview, live session and rendered video as
  AI-generated, and keep the label visible for the whole session.
- **Registered use case.** Keep usage inside the registered use case, a virtual
  assistant or chatbot. Rendered talking-head videos may fall outside it, so
  confirm that before Phase 3.
- **RAI record.** Avatars bring a new output modality, synthetic human likeness,
  on provider surfaces that the 2026-09-03 owner decision doesn't name: custom
  avatar creation and batch avatar synthesis. Enabling them fires
  [review trigger 3](rai-decision-record.md#review-triggers). Before activation the
  owner decides the avatar modality's posture:
  - abuse cases: impersonation of real or public people, depictions of minors,
    sexualized or hateful depictions, and deceptive undisclosed use;
  - disclosure;
  - monitoring and escalation.

  The existing decision directs AI4IA not to block content based on guardrail
  assessments. Whether that also covers avatar prompts is part of the same
  decision.

## How it fits AI4IA's contracts

| Surface | Contract | Design |
| --- | --- | --- |
| Create, status and delete | Model traffic goes through the gateway (rule 1) | SimpleL7Proxy → APIM → the avatar home account, on a dedicated APIM API with exact operations and managed-identity auth. There is no catch-all route, and no user-supplied host or path. |
| Preview image and rendered video | Durable, owner-scoped media | Each artifact is copied once into AI4IA Blob and served owner-scoped. The provider's SAS link is fetched once, with HTTPS, host, size, type and no-redirect checks, then discarded. It is never stored, logged or returned. |
| Avatar records | Cosmos is canonical, per user (rule 4) | A user-owned record maps the owner to an opaque provider id, the prompt, attributes, state and artifact ids. The provider namespace is shared by every user, so AI4IA never lists it to users and never accepts a client-supplied provider id. |
| Feature gate | Server-authoritative (rule 3) | A default-off setting with fail-closed `validate_runtime` prerequisites, Bicep and azd wiring, prerequisite validation and a group-policy restriction. The web app hides the UI but never enforces. |
| Home account, base model and API versions | Catalog-driven (rule 2) | One catalog-owned avatar block, preferably next to the Speech Voice Live provider in `infra/voice-providers.json`, with its generator and `--check`. No account, region, model or version is hardcoded. |
| Real-time session | Existing relay → APIM path (rule 1) | The existing `/api/voice/live` relay, on the Speech Voice Live provider only. |
| Real-time media | New exception to rule 1 | WebRTC media flows directly between the browser and Microsoft's media relay. This needs owner approval and an `AGENTS.md` amendment before Phase 2. |
| Cost | Owner admission (rule 8) | Priced per avatar and per minute. Admission happens before any provider spend. Unpriced paths stay cost-unknown and refuse under caps, and accepted but unrecoverable work is never refunded. |
| Evidence | Receipts; no secret sprawl (rules 6 and 7) | Record a bounded prompt, attributes, the outcome and cost evidence. Record avatar ids as short prefixes, because the receipt redactor in `app/api/src/ai4ia_api/agents/tools.py` masks tokens of 32 or more characters. Never record ICE credentials or SDP. |

## Current seams the phases change

- **The relay, `app/api/src/ai4ia_api/routers/realtime.py`:**
  - `normalize_speech_client_frame()` rebuilds `session.update` for Speech Voice
    Live and keeps only server-owned and bounded fields. So a client `avatar`
    object is dropped today. Keep it that way: the relay must inject the avatar
    block itself.
  - There is no client event allowlist, so `session.avatar.connect` passes through
    today. Phase 2 adds explicit handling.
  - `session.updated` and `session.avatar.connecting` are forwarded unchanged, and
    frames are not logged. The TURN credentials in the ICE servers must stay out of
    logs, receipts and telemetry.
  - Provider error frames keep a bounded `code`, so `avatar_verification_failed`
    can be mapped.
  - Realtime usage is recorded as one call with unknown usage, capped by
    `realtime_max_session_seconds`. There is no duration meter.
- **The voice catalog.** `infra/voice-providers.json` pins Speech Voice Live to
  `/voice-live/realtime` at `2026-04-10`, which documents the photo avatar
  component.
- **The web client.** `app/web/src/lib/voiceLive.ts` is WebSocket and audio only,
  with no `RTCPeerConnection` or video element. The Content Security Policy in
  `app/web/src/proxy.ts` sets no `connect-src` or `default-src`, so it doesn't
  block WebRTC, and `img-src` allows `self`, `data:` and `blob:`.
- **Video generation.** The pattern for async provider jobs is in
  `app/api/src/ai4ia_api/videos/service.py`,
  `app/api/src/ai4ia_api/videos/artifacts.py` and
  `app/api/src/ai4ia_api/videos/availability.py`: provider-side job state, bounded
  polling, owner-scoped Blob artifacts and a shared availability predicate. Media
  today arrives as base64 or through gateway content routes. No code fetches a
  provider SAS link yet.
- **APIM's permissions.** APIM's identity holds Azure OpenAI User and Cognitive
  Services User on every regional account, plus Foundry User on the Speech Voice
  Live account (`infra/modules/gateway.bicep`). Cognitive Services User grants all
  Cognitive Services data actions, so it may already authorize the avatar surfaces.
  Phase 0 checks that before any new grant.
- **APIM routing.** No APIM operation exposes the custom avatar or batch synthesis
  paths today. `scripts/gen-gateway-policy.py` routes catalog deployments, not
  operations without a deployment.
- **Foundry projects.** Each regional account is created with a Foundry project
  (`infra/modules/foundry.bicep`), which can own the `{project}_PhotoAvatar` avatar
  project.

## Phases

### Phase 0: decisions, access and spikes

This phase makes no user-visible change and provisions nothing.

- **Approval.** Record the Limited Access approval, with the evidence held
  outside the repository like the guardrails approval. Note which capability
  signal changes when it lands: either the account capability list or the custom
  avatar features read. The runtime check binds to that signal.
- **RAI.** Get owner re-approval under trigger 3 for the avatar modality. It
  covers abuse cases, disclosure, the feedback channel, escalation and the prompt
  posture.
- **Use case.** Confirm that the registered use case covers each phase.
- **WebRTC spike.** Run the media path end to end with the Voice Live avatar
  sample, against the chosen home account at AI4IA's pinned `2026-04-10`. It
  should:
  - measure start-up latency;
  - confirm which audio travels over WebRTC and which events stay on the
    WebSocket;
  - confirm that interruption works and that the catalog's default voice pairs
    with a photo avatar;
  - check TURN reachability from the networks users are on.
- **Authorization check.** Confirm whether a principal holding only Cognitive
  Services User can call the avatar create, status, delete and batch operations.
  Grant a Speech role scoped to the home account only if that check fails.
- **Choices.** Choose the home account and the gateway exceptions (see the
  decisions below).

**Exit:** the decisions are signed off, an `AGENTS.md` amendment is drafted for the
approved exceptions, and the prices are sourced.

### Phase 1: create, preview, list and delete

- **Gate.** A default-off setting and azd variable, for example
  `AI4IA_PHOTO_AVATARS_ENABLED`. Its fail-closed prerequisites are durable Blob,
  the Cosmos record container, a catalog home account and the capability check.
  A group-policy restriction limits it to a pilot group.
- **Owner-scoped router:**
  - create returns 202 with the new record;
  - status polls the provider, with a bound;
  - list reads only the owner's Cosmos records;
  - delete idempotently removes the provider avatar (a 404 counts as gone), the
    Blob preview and the record;
  - preview images are served owner-scoped.
- **Provider adapter.** One adapter module owns the undocumented creation
  contract: paths, api-version, id pattern, enums and state machine, backed by
  synthetic contract fixtures. An unknown state reads as pending or unknown, never
  as success.
- **Ids.** Avatar ids are server-generated, opaque and match the provider pattern.
  The user's display name is stored separately.
- **Prompt handling:**
  - the prompt is bounded and the attributes are validated;
  - the user attests that the character is fictional, an adult, and not modeled on
    a real or identifiable person;
  - the provider's safety outcome is recorded;
  - any AI4IA enforcement follows the Phase 0 decision.
- **Preview.** On success the preview PNG is copied into Blob straight away. The
  SAS link is never stored, logged or returned.
- **Cost.** Admission and a per-avatar meter run before the create call. A create
  whose acceptance is unknown is never repeated unless a status read shows the
  avatar is absent, so the owner is never charged twice.
- **Web.** An avatar gallery with create, status, preview and delete. Items carry
  AI-generated labels and a report-a-problem link, which meets the feedback
  obligation. The gallery is hidden when the feature is unavailable.
- **Degradation.** If the capability disappears, creation refuses and existing
  avatars show as unavailable. Records are never deleted automatically.
- **Tests.** Each case is paired with a control, and each guard is
  mutation-proven:
  - another user cannot read, delete or use an avatar;
  - with the flag off creation refuses, and with it on creation succeeds;
  - a missing capability degrades, and a present one succeeds;
  - a lookalike host for the preview fetch is refused, and the provider host is
    accepted;
  - the SAS link never reaches responses or logs, checked with a positive capture
    control;
  - an unpriced, capped path refuses, and a priced one succeeds.

#### Phase 1 HTTP contract

Router `app/api/src/ai4ia_api/routers/photo_avatars.py`; models in
`app/api/src/ai4ia_api/photo_avatars/models.py`. Every route needs an authenticated
user, and every route except `/config` answers 404 `photo_avatars_disabled` while the
flag is off. Errors use the shared body `{"detail", "code", "correlation_id"}`, plus
`reason` on 503 unavailability and `Retry-After` where noted.

| Route | Success | Refusals (`code`) |
| --- | --- | --- |
| `GET /api/photo-avatars/config` | 200 `PhotoAvatarConfig`, always; only `enabled`, `available`, `reason` and `canCreate` are set while off | none |
| `GET /api/photo-avatars` | 200 `{"avatars": [...]}`, the caller's records, newest first, read from Cosmos only | `photo_avatars_disabled` |
| `POST /api/photo-avatars` | 202 `PhotoAvatar` whenever a record was created; its `status` carries the outcome | 422 `validation_error`, `invalid_photo_avatar_request`, `attestation_outdated`; 409 `avatar_limit_reached`; 429 `daily_creation_limit` with `Retry-After`; 403/429 entitlement refusals; 403 `policy_denied`; 503 `photo_avatars_unavailable` with `reason`; 503 `cost_unknown_under_cap`; `hard_quota_refused` |
| `GET /api/photo-avatars/{id}` | 200 `PhotoAvatar`, reconciled with at most one rate-limited provider read | 404 `not_found` |
| `GET /api/photo-avatars/{id}/preview` | 200 `image/png`, private caching, `X-AI4IA-Synthetic-Media: ai-generated` | 404 `not_found`; 403 `policy_denied` |
| `DELETE /api/photo-avatars/{id}` | 204; repeating it, or an unknown id, is also 204 | 409 `avatar_confirming` with `Retry-After`; 502 `provider_delete_failed` (the record stays `deleting`, and repeating the call finishes it) |
| `POST /api/photo-avatars/{id}/reports` | 202 `{"id", "avatarId", "reason", "createdAt"}` | 404 `not_found`; 422; 429 `report_limit` |

`{id}` is an opaque 32-character lowercase hex record id; any other shape is 404.
The provider's avatar id never appears in a request or a response.

The create body is `displayName` (1-60 characters, shown only in AI4IA), `prompt`,
the optional `gender`, `age`, `ethnicity` and `style` values listed by `/config`,
and an `attestation` whose `version` matches `/config` and whose `fictional`,
`adult` and `notRealPerson` are all `true`.

A `PhotoAvatar` carries `id`, `displayName`, `prompt`, `attributes`, `status`,
`failure`, `preview`, `disclosure` (`aiGenerated: true` and a label), `cost` (the
estimate recorded at dispatch, never repriced), `usable`, `reported`, `createdAt`,
`updatedAt` and `readyAt`. Its `status` is one of:

- `creating`: reserved, and the provider outcome isn't recorded yet;
- `generating`: accepted and still being generated;
- `confirming`: the create outcome is unknown, and a status read reconciles it;
- `ready`: the preview is stored;
- `failed`: terminal, with a `failure.code`;
- `deleting`: deletion started.

`PhotoAvatarConfig.reason` is one of `available`, `disabled`, `storage_unavailable`,
`residency_unsupported`, `policy_denied`, `policy_unavailable`,
`capability_unavailable` or `capability_unknown`. It is for display only; the server
applies the same check again when a create runs. `/config` also returns the limits
and current usage, attribute options, the attestation text, the disclosure label,
the per-avatar price estimate, and the report reasons with Microsoft's report link.

#### Phase 1 web experience

The gallery is `app/web/src/components/PhotoAvatarsPanel.tsx`, with its client in
`app/web/src/lib/photoAvatars.ts`. Like the API it is default-off. The sidebar
shows a **Photo avatars** entry only while `GET /api/photo-avatars/config` reports
`enabled` for the signed-in owner, and a failed read hides it. No web environment
variable or Bicep change is involved. This is visibility only: every route re-checks
the gate, and enabling the flag outside local development still waits for the
Limited Access approval.

- **Create.** A name and a description, counted against `promptMaxChars`. Style, age,
  gender and ethnicity are optional and start unspecified. The three attestation
  statements appear as `/config` words them, and Create stays disabled until each is
  confirmed. The request sends the attestation `version` from `/config`. The form shows
  the per-avatar estimate, or "unknown" when there is no price, and the current
  limits. A create whose outcome is unknown is never repeated; the gallery re-reads
  the list instead.
- **Status.** Pending records poll `GET /{id}` with backoff from 2 to 15 seconds, for
  at most three minutes and never while the tab is hidden. Polling stops on `ready`,
  on `failed`, or when the gallery closes.
- **Preview.** The bytes come only from the record's own
  `/api/photo-avatars/<id>/preview`, fetched through `apiFetch` into a `blob:` URL.
  Any other `preview.url` gets no image and no request. Every preview carries the
  `disclosure.label` badge. `app/web/src/components/PhotoAvatarPreview.tsx` packages
  this for later surfaces.
- **Report a problem.** The server's reasons, optional details bounded by
  `detailsMaxChars`, and a link to `feedback.microsoftReportUrl`. This is the feedback
  channel the Limited Access terms require.
- **Delete.** An inline confirmation, then optimistic removal. `avatar_confirming`
  puts the avatar back and disables Delete for its `Retry-After`.
- **Unavailable states.** Each `reason` and refusal code has its own explanation,
  including a pending Limited Access approval (`capability_unavailable`). Existing
  avatars stay listed while creation is unavailable.

### Phase 2: real-time conversation

- **Server-owned avatar.** This works on the Speech Voice Live provider only.
  When the browser opens `/api/voice/live`, it names one of its own avatar
  records. The relay:
  - re-checks ownership, state, flag and capability at connect time;
  - keeps dropping any client `avatar` field;
  - injects the server-owned avatar block, with the catalog base model and a
    catalog voice.
- **Handshake.** The relay accepts `session.avatar.connect` only when it
  configured an avatar for the session. SDP size and the number of attempts are
  bounded, and the event is refused otherwise. ICE servers and
  `session.avatar.connecting` are forwarded without being logged.
- **Web.** A peer connection built from the ICE servers, a video element, and a
  persistent AI-generated label. The client falls back to audio only if WebRTC
  fails.
- **Cost.** A per-minute avatar meter, measured by the server from avatar
  connection to close. It is capped by `realtime_max_session_seconds` and by an
  avatar-minute limit.
- **Errors.** `avatar_verification_failed` maps to "avatar unavailable", and the
  record is marked for re-verification.
- **Contract.** The `AGENTS.md` exception for the media plane lands before or
  with this phase.
- **Tests.** Extend `app/api/tests/test_realtime_logic.py`,
  `app/api/tests/test_realtime_api.py` and
  `app/api/tests/test_realtime_staged_api.py`:
  - client avatar injection is dropped, paired with server injection;
  - connect is refused without a configured avatar, paired with an allowed one;
  - ownership is re-checked at connect;
  - no credential or SDP reaches the logs, checked with a positive capture
    control.

### Phase 3 (optional): rendered talking-head videos

Only if the registered use case covers it.

- **Reuse.** Reuse the video generation seams: the availability predicate,
  durable owner-scoped Blob artifacts and bounded polling. `customized: true`,
  `photoAvatarBaseModel` and the voice come from the catalog. Output is an h264
  MP4 with a time to live.
- **Download.** `outputs.result` is fetched with the same bounded, validated fetch
  as the preview image. `destinationContainerUrl` would need a SAS to an account
  open to all networks, which conflicts with AI4IA's storage posture.
- **Cost.** A per-minute bound derived from the input length is reserved before
  the job, and settled against the reported duration.
- **Chat tool.** Optionally, a chat tool with approval and execution-time
  re-checks (rule 5), with receipts.

### Later

Each of these needs its own review:

- scene and background settings (a transparent background needs webm and vp9);
- HD avatars;
- custom voices;
- avatars bound to published agents or workflows.

## Owner decisions

1. **Approval gate.** Enable outside local development only after the Limited
   Access approval. Recommended: yes. Live calls always sit behind the capability
   check, and tests use fakes.
2. **RAI.** Re-approve under trigger 3 and choose the avatar prompt posture.
3. **Media-plane exception.** Allow WebRTC media directly between the browser and
   Microsoft's media relay for Phase 2.
4. **Artifact fetch.** Allow the bounded fetch of provider-issued Blob SAS links
   for previews and rendered videos. This is recommended over
   `destinationContainerUrl`.
5. **Home account.** The eastus2 regional account, which the catalog already uses
   for Speech Voice Live and which supports every avatar feature. Add an EU home
   in swedencentral only if residency requires it. Each avatar stays bound to its
   home account.
6. **RBAC.** Rely on APIM's existing Cognitive Services User role if the Phase 0
   check passes. Otherwise, grant a Speech role scoped to the home account only.
7. **Access.** Restrict creation and use to a pilot group at first.
8. **Limits and pricing.** Set caps on avatars per user, creations per day,
   real-time avatar minutes and batch minutes. Use standard avatars, not HD, at
   first.
9. **Voices.** Allow only the catalog's Azure standard voices.
10. **Retention.** Avatars last until their owner deletes them. Offboarding also
    deletes the provider avatars. Rendered videos expire.
11. **Phase 3.** Decide in or out, after the use-case check.

## Risks

- **API drift.** The creation API is undocumented and can change or disappear.
  The adapter isolates it, contract fixtures detect drift, and implementers
  should watch for a documented version.
- **Access enforcement.** Enforcement of the access gate can change at any time.
  Fail-closed capability checks and graceful degradation handle that.
- **WebRTC.** The media path is untested, and corporate networks may block TURN.
  Phase 0 tests it end to end, and the client falls back to audio only.
- **Cross-user access.** `avatar_verification_failed` checks only that an avatar
  exists on the resource, not who owns it. AI4IA's owner-scoped records are the
  only boundary between users.
- **Home account lock-in.** Each avatar is bound to one account, so changing the
  home region means re-creating avatars.
- **Cost.** Every creation, real-time minute and rendered minute is billable, so
  limits and admission come first.

## Sources

Checked 2026-09-25:

- [How to create a custom photo avatar](https://learn.microsoft.com/azure/ai-services/speech-service/text-to-speech-avatar/custom-photo-avatar-create)
- [What is custom text to speech avatar?](https://learn.microsoft.com/azure/ai-services/speech-service/text-to-speech-avatar/what-is-custom-text-to-speech-avatar)
- [Batch synthesis properties for text to speech avatar](https://learn.microsoft.com/azure/ai-services/speech-service/text-to-speech-avatar/batch-synthesis-avatar-properties)
- [Voice Live how-to: text to speech avatar](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live-how-to#azure-text-to-speech-avatar)
- [Voice Live `2026-04-10` API reference](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live-api-reference-2026-04-10)
- [Voice Live `2026-07-15` API reference](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live-api-reference-2026-07-15)
- [Supported regions for Azure Speech](https://learn.microsoft.com/azure/ai-services/speech-service/regions)
- [Limited Access for text to speech](https://learn.microsoft.com/azure/foundry/responsible-ai/speech-service/text-to-speech/limited-access)
- [Disclosure design guidelines for synthetic voices](https://learn.microsoft.com/azure/ai-foundry/responsible-ai/speech-service/text-to-speech/concepts-disclosure-guidelines)
- [Microsoft Enterprise AI Services Code of Conduct](https://learn.microsoft.com/legal/ai-code-of-conduct)
- [Azure Speech pricing](https://azure.microsoft.com/pricing/details/cognitive-services/speech-services/)
- [Voice Live avatar sample](https://github.com/Azure-Samples/cognitive-services-speech-sdk/tree/master/samples/js/node/web/voice-live-avatar)
