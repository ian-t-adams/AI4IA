# Custom photo avatars: design and phased plan

> **Status (2026-09-26): owner-approved for enablement.** Phase 1 (create from a
> description, then status, preview, list, delete and report) and Phase 2 (real-time
> conversation on the existing Voice Live WebSocket) are implemented. Phase 2 adds no
> gate of its own: it inherits photo avatars, Speech Voice Live and the capability.
> On 2026-09-26 the owner:
>
> - approved enabling both phases in production;
> - re-approved the annotate-only posture for avatar prompts and live sessions under
>   [RAI review trigger 3](rai-decision-record.md#review-triggers);
> - reported the Limited Access approval for custom text to speech avatar as held
>   (the evidence stays outside the repository);
> - took ownership of the report queue.
>
> Creation and live sessions still work only while the home account itself reports
> the Limited Access capability. Until it does, the API reports
> `capability_unavailable`, and nothing is created or billed. The enablement checks
> are in [the runbook](runbooks/feature-enablement.md#custom-photo-avatars). Phase 3
> (rendered videos) is not built, and this approval does not cover it.

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
| Voice Live session | With the default `output_protocol: webrtc`, `session.update` with a custom photo avatar returned WebRTC ICE servers; AI4IA does not use that mode. With `output_protocol: websocket` at the pinned `2026-04-10` (spike and live check, 2026-09-26), `session.updated` lists the `avatar` modality, echoes the avatar block with `ice_servers: null`, and the avatar streams on the same WebSocket as `response.video.delta`. An unknown avatar name is refused with `avatar_verification_failed` before any `session.updated`. |
| Non-human prompt | A cartoon dog was accepted and animated, but the animation model humanized it. Stylized 3D humans render well; realistic humans look best. |

**WebSocket avatar media (2026-09-26).** Measured with AI4IA's own relay logic
against the test resources, in two bounded sessions:

- **Stream format.** Each `response.video.delta` carries base64 fragmented MP4: an
  `ftyp`+`moov` init segment in the first delta, then `moof`/`mdat` fragments.
  - Video: H.264 High level 3.0 (`avc1.64001E`), 512×512 at 25 fps.
  - Audio: AAC-LC 16 kHz mono (`mp4a.40.2`), inside the same stream. There is no
    `response.audio.delta`.
- **Frame size.** The largest whole frame was 24,841 characters. The relay bounds
  frames at 256 KiB.
- **Idle streaming.** Video starts as soon as `session.updated` confirms the avatar,
  before any response, and idle frames keep streaming until the session closes:
  25 fps at about 566 kbps.
- **Speaking markers.** `session.avatar.switch_to_speaking` and
  `session.avatar.switch_to_idle` bracket speech. `response.done` arrives while the
  avatar is still speaking its buffered video.
- **Interruption.**
  - `response.cancel` during speech ended the response (`cancelled`) in about 0.1
    seconds, and the avatar was idle in about 0.2 seconds.
  - `output_audio_buffer.clear` answered `output_audio_buffer.cleared` in about 0.1
    seconds, with the same effect.
- **Provider id in the echo.** The `session.updated` echo includes the avatar's
  `character`, which is the provider id, so the relay scrubs it.
- **Browser playback.** Chromium played the captured deltas through AI4IA's
  MediaSource player at 512×512, using the codec string derived from the stream's
  own `avcC` box.

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
    "avatar": {
      "type": "photo-avatar", "model": "vasa-1", "character": "{avatarId}",
      "customized": true, "output_protocol": "websocket"
    }
  }
}
```

With `output_protocol: websocket` there is no handshake. The service streams the
avatar on the same WebSocket as `response.video.delta` events. The default `webrtc`
mode instead returns ICE servers with TURN credentials and needs
`session.avatar.connect` with an SDP offer; AI4IA does not use it.

The avatar component (`type`, `model`, `character`, `customized`, `output_protocol`,
`ice_servers`) is documented in the `2026-04-10` reference, which AI4IA pins, and in
`2026-07-15`. The WebSocket measurements above used `2026-04-10`.

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
| Real-time session | Existing relay → APIM path (rule 1) | The existing `/api/voice/live` relay, on the Speech Voice Live provider only. The browser names an owned record; the relay resolves it and injects the server-owned avatar block. |
| Real-time media | Existing relay → APIM path (rule 1); no exception | `output_protocol: websocket`: the avatar's video and speech arrive as `response.video.delta` frames on the same governed WebSocket, FastAPI relay → APIM Voice Live API → Foundry. Frames are bounded and never logged or stored. There is no WebRTC, no ICE or TURN credential and no browser media plane. |
| Cost | Owner admission (rule 8) | Priced per avatar, and per second of live avatar time at $0.60 per minute. Admission happens before any provider spend. Unpriced paths stay cost-unknown and refuse under caps, and accepted but unrecoverable work is never refunded. |
| Evidence | Receipts; no secret sprawl (rules 6 and 7) | Record a bounded prompt, attributes, the outcome and cost evidence. Record avatar ids as short prefixes, because the receipt redactor in `app/api/src/ai4ia_api/agents/tools.py` masks tokens of 32 or more characters. Never record video frames or the provider id. |

## Current seams the phases change

- **The relay, `app/api/src/ai4ia_api/routers/realtime.py`, with its pure avatar
  helpers in `app/api/src/ai4ia_api/realtime_avatar.py`:**
  - `normalize_speech_client_frame()` rebuilds `session.update` for Speech Voice
    Live and keeps only server-owned and bounded fields, so a client `avatar`
    object is dropped. Phase 2 keeps that and injects the avatar block itself, last
    in the rewrite chain.
  - Before Phase 2 there was no client event allowlist, so `session.avatar.connect`
    passed through. The relay now refuses every client `session.avatar.*` event on
    every provider.
  - Server frames are forwarded without being logged. In an avatar session the
    relay scrubs the provider id from the `session.updated` echo and every other
    non-video frame, and bounds `response.video.delta` frames.
  - Provider error frames keep a bounded `code`, and `avatar_verification_failed`
    becomes a stable client error.
  - Realtime usage is one call with unknown usage, capped by
    `realtime_max_session_seconds`. An avatar session adds its own per-second meter
    row.
- **The voice catalog.** `infra/voice-providers.json` pins Speech Voice Live to
  `/voice-live/realtime` at `2026-04-10`, which documents the photo avatar
  component.
- **The web client.** `app/web/src/lib/voiceLive.ts` was WebSocket and audio only.
  In an avatar session it now feeds `response.video.delta` to the MediaSource player
  in `app/web/src/lib/avatarVideo.ts` and plays no PCM. The Content Security Policy
  in `app/web/src/proxy.ts` sets no `media-src` or `default-src`, so the player's
  `blob:` MediaSource URL needs no policy change. There is no `RTCPeerConnection`.
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
- **Media spike (done 2026-09-26).** At AI4IA's pinned `2026-04-10`, with the
  catalog's default DragonHD voice, `output_protocol: websocket` delivers the avatar
  on the existing WebSocket (see
  [WebSocket avatar media](#verified-behavior-2026-09-25)). That keeps media on the
  relay → APIM path, so no media-plane exception and no TURN reachability check
  are needed. WebRTC stays untested and unused.
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
  - delete idempotently removes the provider avatar (the provider's own NotFound
    counts as gone), the Blob preview and the record;
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
| `DELETE /api/photo-avatars/{id}` | 204; repeating it, or an unknown id, is also 204 | 409 `avatar_confirming` with `Retry-After`; 409 `avatar_home_changed` (the avatar belongs to a previous home account; an operator removes it); 502 `provider_delete_failed` (the record stays `deleting`, and repeating the call finishes it); 503 `delete_incomplete` (repeating the call finishes it) |
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
- `generating`: accepted, and still being generated or waiting for its preview to
  be stored;
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

A `PhotoAvatar` also carries `needsReverification`. It is set when a live session
reported that the avatar failed verification, and `usable` stays false until a
later status read re-verifies the avatar.

#### Phase 1 implementation decisions

- **Gateway.** A separate, flag-gated APIM API, `ai4ia-photo-avatars-v1`, has six
  exact operations: the features read, the avatar project read and create, and the
  avatar create, read and delete. It has no list operation and no wildcard.
  `scripts/gen-voice-provider-catalog.py` renders its policy,
  `infra/policies/photo-avatars.xml`, from the catalog, and
  `scripts/gen-gateway-policy.py` validates it. The policy:
  - binds the API-scoped proxy subscription;
  - admits only AI4IA-issued avatar ids (`ai4ia-` plus 20 hex characters), no
    caller query string and, for create, a JSON object of catalog-enumerated
    properties up to 16 KiB;
  - re-serializes that validated body and owns the avatar project body;
  - pins the provider paths and api-version;
  - strips caller and proxy headers before managed-identity authentication, and
    forwards exactly once.
- **Proxy.** A named proxy host, `Host-photoavatars`, not `Host3`, because the proxy
  stops reading numbered hosts at the first gap and `Host2` is conditional. Its
  exact non-stripping path makes it the only candidate for avatar requests.
  FastAPI reuses its existing proxy-ingress credential.
- **No create retry.** The create is one admitted PUT with an `S7PTTL`, so a queued
  request can't be sent late. FastAPI never resends it:
  - an accepted create is billed;
  - a definite rejection (400, 401, 403, 404, 409, 413, 415, 422 or 429) fails without
    a charge;
  - anything else becomes `confirming`. A status read settles it: the provider's
    state is adopted, or the record fails as `not_created` only after the proxy
    time to live plus a four-minute margin.

  Only an admission refusal raised before the request leaves (hard quota or
  policy) releases the reservation. Any other fault after dispatch, including a
  reply that can't be parsed or classified, leaves the create `confirming` and
  metered as cost-unknown. A 2xx reply is accepted by its status alone, even when
  its body can't be read.
- **Absence.** Only the provider's own `404 {"error":{"code":"NotFound"}}` proves
  that an avatar or the avatar project is gone. Any other 404, such as APIM's own
  `{"statusCode":404}` for a missing or rolled-back API, is an unknown answer: a
  status read changes nothing, and a delete fails with `provider_delete_failed`.
  The generated policy's own refusals are 400 and 502 with AI4IA codes, and a test
  parses the policy to keep it that way.
- **Home changes.** Each record keeps the home region it was created in. After
  the catalog home changes, APIM routes to the new account, whose answers say
  nothing about older avatars, even a well-formed NotFound. Those records are
  never read, reconciled or re-verified and never become terminal. They report
  `usable: false`, and deletion refuses with 409 `avatar_home_changed` until an
  operator removes them from the previous account (see the runbook).
- **Avatar project.** It is created lazily and idempotently at runtime, through the
  same gateway: a GET, then a PUT only on 404. This reuses APIM's existing
  Cognitive Services User role, needs no deploy-identity role, and never replaces an
  existing project. It is the one unmetered provider write. An AST inventory test
  pins the adapter's writes: the admitted create, this setup call and the delete.
- **Ids.** Record ids are opaque 32-character hex strings. Provider ids are
  `ai4ia-` plus 20 hex characters: generated on the server, never accepted from or
  returned to clients, and shorter than the receipt redactor's 32-character
  threshold.
- **Capability.** The probe reads the account's features through the gateway, 5
  seconds at most, and caches the result for 60 seconds (15 seconds after an error).
  Only exact membership of the catalog's `requiredFeature` counts. An error, a
  timeout or an unexpected shape reads as unknown, and all of them refuse creation.
- **Availability.** One predicate checks flag, storage, residency, policy and
  capability, in that order. `/config`, each record's `usable` flag and the create
  path share it, and the live resolver re-runs it with `avatar.use` enforced.
- **Limits.** An owner-partition ledger in the `photoAvatars` container holds the
  current record ids and the rolling creation and report times. A create is
  reserved in the same Cosmos batch as its ledger update, so the limits hold under
  concurrent requests.
- **Pilot access.** A group-policy `avatars` domain with two actions: `create`,
  which is consumption, and `use`. Owner reads, status, deletion and reports need
  no grant. A `zones` restriction makes creation `policy_unavailable`: zones
  describe model processing scope, and avatar residency comes from the catalog
  home instead. The dispatch seam refuses the same actor, through the same shared
  rule (`avatar_creation_zone_scoped`), so `/config` never advertises a creation
  that dispatch would refuse. Using an existing avatar is unaffected.
- **Metering.** One usage row per dispatched create, under a deterministic id:
  `photo-avatar-create-<record id>`. An accepted create records a known $2
  estimate; an unknown outcome records cost-unknown, never free. Hard admission
  covers creation as a request-only surface.
- **Preview.** The one direct egress. The catalog names one exact provider storage
  host. The fetch pins the checked public address, refuses redirects, streams under
  a byte cap and requires a PNG within the catalog's dimension bound. It drives the
  pinned transport directly, because the HTTP client logs full request URLs and
  this one carries the SAS signature. Failures split by what they prove:
  - a link on any other host is never fetched. The avatar stays `generating`,
    status reads drop to the slow interval, and a `host_not_in_catalog` security
    event names the problem, so a corrected catalog recovers the paid avatar;
  - a DNS lookup that fails or times out, a network or stream error, or a failed
    Blob write is retried on a later status read;
  - a malformed link, a non-public address, a redirect, or content that isn't a
    PNG within bounds fails the avatar with `preview_rejected`.

  A status read that stores a preview after a delete has won removes that preview
  again, and deleting a record that no longer exists also removes a preview left
  behind.
- **Live sessions.** `photo_avatars/live.py` gives the Phase 2 relay
  `resolve_live_avatar(state, user, record_id)`. The returned `LiveAvatarGrant` is
  the only way the provider id leaves the package. Refusals carry stable codes.
  `mark_live_avatar_verification_failed` records a Voice Live verification failure:
  live use is refused until, after a five-minute cooldown, a fresh provider read
  still finds the avatar `Succeeded`.

#### Read-only observations, 2026-09-26

A read-only pass against the test resources, GET requests only, confirmed the
shapes the adapter's synthetic fixtures use:

- The features read returns a JSON array of strings. It did not contain
  `CustomAvatar`, because the registration is pending.
- An unknown avatar or project returns `404 {"error":{"code":"NotFound"}}`.
- **Avatar ids are scoped to the account, not the avatar project.** A read
  through a different project's path returned an existing avatar. So the
  project segment is not an isolation boundary, and that is why APIM accepts only
  AI4IA-issued ids.
- `promptImageUri` is a user-delegation SAS on a provider storage account named
  for its region. A fetch returned `application/octet-stream`: a 1024×1024 RGB
  PNG, about 1.3 MB. A tampered signature returned 403.

The catalog's eastus2 preview host follows the observed naming for westus2. The
account name resolves in DNS, and a one-character variant does not. It has not yet
been seen as an issued preview host, so confirming it is an enablement check. If
the host is wrong, nothing is fetched: the avatar stays `generating` and the API
logs `host_not_in_catalog` until the catalog is corrected.

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
  confirmed. The ticks belong to the attestation `version` they were given for, so a
  refresh that brings new wording clears them. The request sends that `version`. The
  form shows the per-avatar estimate, or "unknown" when there is no price, and the
  current limits.
- **Unknown create outcomes.** A create is never repeated automatically. A 4xx, or
  a 5xx with one of the codes the service raises before anything reaches the
  provider, is a definite refusal. Anything else leaves the outcome unknown: a
  network failure, an unreadable reply, or a 5xx with no code or a generic one. An
  unknown outcome re-reads the gallery and clears the confirmations.
- **Status.** Pending records poll `GET /{id}` with backoff from 2 to 15 seconds, for
  at most three minutes and never while the tab is hidden. Polling stops on `ready`,
  on `failed`, or when the gallery closes. A ready record with `needsReverification`
  shows **Re-verifying…**, and its preview, Report and Delete stay available. Only a
  status read lets the server re-check it after its cooldown, so the gallery reads it
  at once and then once a minute, for six minutes.
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
  `provider_delete_failed` and `delete_incomplete` put it back in `deleting`, so
  Delete can be repeated to finish. `avatar_home_changed` puts it back unchanged and
  explains that an operator must remove it. Delete then stays disabled while the
  gallery is open, and no retry is suggested.
- **Unavailable states.** Each `reason` and refusal code has its own explanation,
  including a pending Limited Access approval (`capability_unavailable`). Existing
  avatars stay listed while creation is unavailable.

### Phase 2: real-time conversation

Implemented in source. It works on the Speech Voice Live provider only, and every
avatar byte stays on the existing governed path: browser → FastAPI
`/api/voice/live` → APIM Voice Live WebSocket API → Foundry.

- **Selection.** The browser adds `?avatar=<record id>`, one of its own 32-hex
  record ids. A query parameter is known at the handshake, so resolution, refusal
  and admission all happen before any upstream connection. It matches the other
  server-validated selectors (`provider`, `model`, `session`, `agent`, `tools`), and
  the id is an opaque, owner-scoped record id, never the provider id.
  - A wrong provider or a malformed id is denied before the socket is accepted.
  - The setup canary actor may never name an avatar.
- **Connect-time checks, on every connection.**
  - Layer 1's `resolve_live_avatar` re-checks ownership, readiness, pending
    re-verification, the home account and the availability predicate. That
    predicate covers the flag, storage, residency, the `avatar.use` policy and
    the capability. Grants are never cached.
  - The relay also requires the Voice Live target region to equal the avatar's
    home region, so the session targets the account that owns the avatar.
  - An unknown live price under a cost cap refuses (`cost_unknown_under_cap`), using
    layer 1's `live_cost_capped` rule.
  - A refusal sends one bounded error and closes with 1008:
    `{"type":"error","error":{"type":"avatar_error","code":"avatar_unavailable","reason":…}}`.
    The reason is allowlisted, and `retry_after_seconds` appears only with
    `needs_reverification`. Nothing is opened upstream and nothing is metered.
    Each of layer 1's live refusal codes has its own reason: a record from a
    previous avatar home (409 `avatar_home_changed`) becomes `home_changed`, and
    only an unrecognized code falls back to the generic `unavailable`.
  - `avatar_live` admission refuses a zones-scoped actor by the same
    `avatar_creation_zone_scoped` rule as creation. `/config` applies that rule
    too, so such an actor is never offered the picker.
- **Server-owned block.** The relay injects
  `{"type":"photo-avatar","model":<catalog base model>,"character":<provider id>,"customized":true,"output_protocol":"websocket"}`
  into every rebuilt `session.update`, after Speech normalization and the
  tool/persona bridge, with the normalizer's catalog voice.
  - No client avatar field survives, including `video`, a background `image_url`
    and `output_audit_audio`.
  - Neither does any client provider id.
- **No WebRTC.** Every client `session.avatar.*` event, including
  `session.avatar.connect`, is refused on every provider: one bounded error, then a
  1008 close. `output_audio_buffer.clear` passes and, like `response.cancel`,
  needs no fresh policy grant.
- **Video forwarding.** `response.video.delta` frames are forwarded verbatim.
  - Every upstream text frame is bounded at 256 KiB before anything parses it. An
    oversized frame ends the session with a bounded error and a 1009 close.
  - Voice Live sends JSON text only, so an upstream binary frame ends an avatar
    session (`avatar_stream_refused`) instead of reaching the browser past the
    bound and the scrub. Other sessions still forward binary frames unchanged.
  - Video is never parsed beyond its event type, logged, receipted or copied into
    telemetry.
  - The provider id is scrubbed from the `session.updated` echo, every other
    frame, and upstream close reasons and error messages before they are
    forwarded, read for log metadata or logged. The completion log and event scrub
    it again as a backstop.
- **Confirmation and errors.**
  - The avatar is confirmed by `session.updated` listing the `avatar` modality, or
    by the first video frame. A `session.updated` that drops a requested avatar
    ends the session, because a client with PCM disabled would otherwise hear
    nothing.
  - `avatar_verification_failed` becomes `avatar_unavailable` /
    `verification_failed`, and `mark_live_avatar_verification_failed` marks the
    record once.
- **Idle and session caps.** Avatar time bills while idle, so:
  - every avatar session is capped by the smaller of
    `realtime_max_session_seconds` and `AI4IA_PHOTO_AVATAR_LIVE_MAX_MINUTES_PER_SESSION`
    (default 10);
  - it ends after `AI4IA_PHOTO_AVATAR_LIVE_IDLE_TIMEOUT_SECONDS` (default 120)
    without conversation. Microphone audio, idle video and output stop events
    (`response.cancel`, `conversation.item.truncate`, `input_audio_buffer.clear`,
    `output_audio_buffer.clear`) never count as conversation. The stop events skip
    the per-send grant check, so they can't keep a revoked session alive either.
  - The countdown holds while the avatar is speaking, between
    `session.avatar.switch_to_speaking` and `switch_to_idle`. `response.done`
    arrives while the avatar is still speaking its buffered answer, and only video
    follows it. The hold lasts at most five minutes, so a speaking state that never
    ends can't keep the session open.
  - The relay sends `ai4ia.avatar.session` with the limits,
    `ai4ia.avatar.idle_warning` shortly before an idle end, and
    `ai4ia.avatar.session_ended` for an idle or cap end.
- **Admission.** Live avatar time is its own hard-quota surface, `avatar_live`,
  which requires `avatar.use`. It is admitted before the unchanged `realtime`
  session admission and before `connector.connect`. The request-count scope
  counts each, and USD caps refuse as for every surface.
  - In an avatar session the per-send policy guard also re-checks `avatar.use`, so
    a revocation stops the next send. The idle watchdog re-runs the same guard
    every 15 seconds. A client that sends nothing the guard checks still loses the
    session: it gets `avatar_unavailable` with `policy_denied` or
    `policy_unavailable`, then a 1008 close.
- **Meter.** Server-measured from avatar confirmation to relay close, in whole
  seconds, rounded up. It is priced at $0.60 per minute billed per second through
  the catalog's `liveBillingModelId` (`photo-avatar-realtime-standard`) in
  `app/api/src/ai4ia_api/data/pricing.json`.
  - The usage row uses provider `azure_speech_photo_avatar`, target
    `photo_avatar_live` and unit `second`, and carries the 8-character record
    prefix in `resourceRef`.
  - An avatar that is never confirmed records no avatar row.
- **Evidence.**
  - The completion log and custom event carry counts only: the record prefix,
    confirmation, billable seconds, cost, video frame counts and sizes, and the end
    reason.
  - A chat-bound session (`?session=`) also gets one `fromCommand` message,
    "Avatar voice session ended.", whose execution receipt carries runtime, tools,
    unknown voice usage and `avatar` evidence.
- **Web.**
  - **Picker.** The Speech voice settings offer an avatar picker only while
    `/api/photo-avatars/config` is enabled and available and the owner has
    `usable` avatars.
  - **Player.** `app/web/src/lib/avatarVideo.ts` derives the codec from the
    stream's `avcC` box, falling back to `avc1.64001E, mp4a.40.2`. It appends
    strictly in order through one bounded queue, evicts played media and chases
    the live edge.
  - **Audio.** Avatar mode plays no PCM. The video is primed in the start gesture
    and is also the speaker.
  - **Fallback.** Without MediaSource or the codec, the session stays voice only.
  - **Stage.** `app/web/src/components/LiveAvatarStage.tsx` keeps the
    `AI-generated` label visible, counts down the idle and session limits, and
    offers **End session**.
    - The label is drawn over the video, so the video can't leave the stage.
      Picture-in-picture, fullscreen, remote playback and the context menu are
      disabled, and entering either mode anyway exits it at once.
    - The countdowns are `role="timer"`, which isn't announced. A separate status
      region speaks once when a warning appears and once about ten seconds before
      the end.
  - **Barge-in.** Barge-in relies on server VAD `interrupt_response` and jumps the
    player to the live edge, only when the reply is actually interrupted. With
    **Interrupt response** off, the avatar keeps talking. There is no manual
    truncate, because avatar mode has no PCM timeline.
- **Tests.** Paired and mutation-proven, in `app/api/tests/test_realtime_logic.py`,
  `app/api/tests/test_realtime_api.py` (with layer 1's real service) and
  `app/api/tests/test_realtime_staged_api.py`, plus vitest tests on synthetic
  fragmented MP4.
- **Not yet proven live:** a server-VAD barge-in with spoken input, echo
  cancellation through the video element's speaker, and the path through AI4IA's
  own APIM. The enablement canary covers the last one.

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
3. **Real-time media.** Resolved without an exception: `output_protocol: websocket`
   keeps the avatar's video and speech on the existing relay → APIM WebSocket path.
   WebRTC would need its own media-plane exception, TURN credentials and review,
   and is not planned.
4. **Artifact fetch.** Allow the bounded fetch of provider-issued Blob SAS links
   for previews and rendered videos. This is recommended over
   `destinationContainerUrl`. It stays the only direct egress: live avatar media
   arrives on the governed WebSocket and needs no fetch.
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

For the Phase 1 implementation, the coordinator adopted the recommended defaults
on 2026-09-25:

- decision 1: default-off, with the fail-closed capability check;
- decision 4: previews only for now;
- decision 5: the eastus2 home account, set in the catalog;
- decision 6: no new role assignment, with a live check at enablement;
- decision 7: an optional group-policy pilot restriction;
- decision 8: caps on avatars per user and creations per day, standard avatars only;
- decision 10: owner-driven, idempotent deletion.

Decision 2 keeps AI4IA's annotate-only posture, which means deterministic product
constraints instead of classifier blocking:

- a bounded prompt;
- validated attributes;
- a required attestation that the character is fictional, an adult and not
  modeled on a real person;
- a recorded provider outcome;
- AI-generated disclosure metadata;
- a report path.

For Phase 2, decision 3 is resolved by the WebSocket output, decision 8 adds the
per-session minute cap and idle timeout, and decision 9 is enforced by the Speech
normalizer, which accepts only the catalog's Azure standard voices.

The trigger-3 re-approval itself is still outstanding. Decision 11 belongs to a
later phase. No offboarding path exists in the repository yet; the runbook
records the operator cleanup until one does.

## Risks

- **API drift.** The creation API is undocumented and can change or disappear.
  The adapter isolates it, contract fixtures detect drift, and implementers
  should watch for a documented version.
- **Access enforcement.** Enforcement of the access gate can change at any time.
  Fail-closed capability checks and graceful degradation handle that.
- **Idle cost and bandwidth.** Avatar video streams at about 566 kbps, and bills,
  for the whole connected session, idle included. The idle timeout, the
  per-session cap, admission and the explicit end control bound it. No
  cross-replica cap limits concurrent avatar sessions per user.
- **Browser support.** The player needs `MediaSource` and the stream's H.264 and
  AAC codecs. iPhone Safari exposes only `ManagedMediaSource`, which the player
  doesn't use yet, so iPhones stay voice only. So do other browsers without the
  codecs. These sessions fall back to voice only before connecting.
- **Provider id in echoes.** Voice Live echoes the avatar's `character`. The relay
  scrubs it from every non-video frame, from upstream close reasons and error
  messages, and, as a backstop, from the completion log and event. A future event
  that carried the id inside video data would need a new rule.
- **Echo cancellation.** Server echo cancellation now has to cope with speech
  played by a buffered video element. This is untested live; headphones avoid it.
- **Cross-user access.** `avatar_verification_failed` checks only that an avatar
  exists on the resource, not who owns it. AI4IA's owner-scoped records are the
  only boundary between users.
- **Home account lock-in.** Each avatar is bound to one account, so changing the
  home region means re-creating avatars, and removing the old ones from the
  previous account is operator cleanup.
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
