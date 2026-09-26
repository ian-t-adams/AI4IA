// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  PHOTO_AVATAR_POLL_BUDGET_MS,
  PHOTO_AVATAR_REVERIFY_BUDGET_MS,
  type PhotoAvatar,
  type PhotoAvatarConfig,
} from "@/lib/photoAvatars";
import { PhotoAvatarsPanel } from "./PhotoAvatarsPanel";

const mocks = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/auth", () => ({ apiFetch: mocks.apiFetch }));

const ID_A = "a".repeat(32);
const ID_NEW = "c".repeat(32);
const ID_PENDING = "d".repeat(32);
const ID_HOSTILE = "e".repeat(32);
const ID_SAS = "f".repeat(32);
const ID_REVERIFY = "9".repeat(32);
const NOW = "2026-09-26T12:00:00Z";
const LATER = "2026-09-26T12:01:00Z";
const LIST = "/api/photo-avatars";
const CONFIG_PATH = "/api/photo-avatars/config";
const previewPath = (id: string) => `/api/photo-avatars/${id}/preview`;

const CONFIG: PhotoAvatarConfig = {
  enabled: true,
  available: true,
  reason: "available",
  canCreate: true,
  limits: {
    maxAvatars: 5,
    avatarCount: 1,
    maxCreationsPerDay: 5,
    creationsInLastDay: 1,
    nextCreationAt: null,
    promptMaxChars: 200,
    displayNameMaxChars: 60,
  },
  attributes: {
    gender: ["Male", "Female"],
    age: ["YoungAdult", "MiddleAged", "Senior"],
    ethnicity: ["Asian", "White"],
    style: ["Realistic", "Stylized3D"],
  },
  attestation: {
    version: "fixture-attestation-3",
    statements: [
      { id: "fictional", text: "Fixture statement: the character is invented." },
      { id: "adult", text: "Fixture statement: the character is over 18." },
      { id: "notRealPerson", text: "Fixture statement: the character resembles no real person." },
    ],
  },
  disclosure: { label: "AI-generated", text: "Fixture disclosure: a synthetic likeness." },
  pricing: { currency: "USD", estimatedUsdPerAvatar: 2, known: true, priceVersion: "fixture-v1" },
  feedback: {
    reasons: ["impersonation", "minor", "sexual", "hateful", "violent", "other"],
    detailsMaxChars: 40,
    microsoftReportUrl: "https://aka.ms/reportabuse",
  },
};

function avatar(overrides: Partial<PhotoAvatar> = {}): PhotoAvatar {
  const id = overrides.id ?? ID_A;
  return {
    id,
    displayName: "Host A",
    prompt: "A friendly host, head and shoulders, plain background.",
    attributes: { gender: null, age: null, ethnicity: null, style: "Realistic" },
    status: "ready",
    failure: null,
    preview: { url: previewPath(id), contentType: "image/png", width: 1024, height: 1024, bytes: 4 },
    disclosure: { aiGenerated: true, label: "AI-generated" },
    cost: { currency: "USD", estimatedUsd: 2, known: true, priceVersion: "fixture-v1", basis: "per_avatar" },
    usable: true,
    reported: false,
    createdAt: NOW,
    updatedAt: NOW,
    readyAt: NOW,
    ...overrides,
  };
}

const READY_A = avatar();
const PENDING = avatar({
  id: ID_PENDING,
  displayName: "Pending host",
  status: "generating",
  preview: null,
  usable: false,
  readyAt: null,
});
const CREATED = avatar({
  id: ID_NEW,
  displayName: "Office guide",
  status: "generating",
  preview: null,
  usable: false,
  readyAt: null,
});
// Ready, but a live session could not verify it: usable is false meanwhile.
const REVERIFYING = avatar({
  id: ID_REVERIFY,
  displayName: "Checked host",
  usable: false,
  needsReverification: true,
});

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function png(): Response {
  return new Response(new Uint8Array([137, 80, 78, 71]), {
    headers: { "Content-Type": "image/png", "X-AI4IA-Synthetic-Media": "ai-generated" },
  });
}

type Handler = (init?: RequestInit) => Response | Promise<Response>;
const routes = new Map<string, Handler>();
function on(method: string, path: string, handler: Handler) {
  routes.set(`${method} ${path}`, handler);
}
function calls(method: string, path: string) {
  return mocks.apiFetch.mock.calls.filter(
    ([input, init]) => String(input) === path && ((init as RequestInit | undefined)?.method ?? "GET") === method,
  );
}
function requestedUrls(): string[] {
  return mocks.apiFetch.mock.calls.map(([input]) => String(input));
}

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function itemFor(name: string): HTMLElement {
  const item = screen.getByRole("heading", { name }).closest("li");
  if (!item) throw new Error(`no gallery item for ${name}`);
  return item;
}

async function fillValidDraft(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Name"), "Office guide");
  await user.type(screen.getByLabelText("Description"), "A calm host in a blue jacket");
}

async function attestAll(user: ReturnType<typeof userEvent.setup>, config = CONFIG) {
  for (const statement of config.attestation!.statements) {
    await user.click(screen.getByRole("checkbox", { name: statement.text }));
  }
}

let objectUrls = 0;
let visibility: DocumentVisibilityState = "visible";

beforeEach(() => {
  routes.clear();
  objectUrls = 0;
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  mocks.apiFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const handler = routes.get(`${init?.method ?? "GET"} ${String(input)}`);
    return handler ? handler(init) : json({ detail: "Not found.", code: "not_found" }, 404);
  });
  on("GET", CONFIG_PATH, () => json(CONFIG));
  on("GET", LIST, () => json({ avatars: [READY_A] }));
  for (const id of [ID_A, ID_NEW, ID_PENDING, ID_HOSTILE, ID_SAS, ID_REVERIFY]) {
    on("GET", previewPath(id), () => png());
  }
  on("GET", `${LIST}/${ID_NEW}`, () => json(CREATED));
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => `blob:photo-avatar-${++objectUrls}`),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  delete (document as { visibilityState?: unknown }).visibilityState;
});

describe("availability", () => {
  it.each([
    ["capability_unavailable", /Limited Access approval for custom avatars/],
    ["policy_denied", /access policy doesn't allow you to create/],
    ["policy_unavailable", /access policy couldn't be checked/],
    ["storage_unavailable", /Avatar storage is unavailable/],
    ["residency_unsupported", /data residency settings/],
    ["capability_unknown", /approval couldn't be confirmed/],
  ] as const)("explains %s and keeps existing avatars listed", async (reason, text) => {
    on("GET", CONFIG_PATH, () => json({ ...CONFIG, available: false, canCreate: false, reason }));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText("Creating avatars is unavailable")).toBeInTheDocument();
    const create = screen.getByRole("button", { name: "New avatar" });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription(text);
    expect(screen.getByRole("heading", { name: "Host A" })).toBeInTheDocument();
  });

  it("offers creation without a notice when the server reports it available", async () => {
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "New avatar" })).toBeEnabled();
    expect(screen.queryByText("Creating avatars is unavailable")).toBeNull();
    expect(screen.getByText(/1 of 5 avatars · 1 of 5 creations in the last 24 hours/)).toBeInTheDocument();
  });

  it("shows that the feature was turned off without listing anything", async () => {
    on("GET", CONFIG_PATH, () => json({
      enabled: false, available: false, reason: "disabled", canCreate: false,
      limits: null, attributes: null, attestation: null, disclosure: null, pricing: null, feedback: null,
    }));
    on("GET", LIST, () => json({ detail: "Photo avatars are disabled.", code: "photo_avatars_disabled" }, 404));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    expect(await screen.findByText("Photo avatars are turned off in this deployment.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New avatar" })).toBeNull();
  });

  it.each([
    [{ avatarCount: 5 }, /5 of 5 avatars, the most you can keep\. Delete one/],
    [{ creationsInLastDay: 5, nextCreationAt: "2026-09-27T09:30:00Z" }, /used 5 of 5 creations in the last 24 hours\. You can create another after /],
  ])("explains a full limit next to a disabled New avatar (%o)", async (limits, text) => {
    on("GET", CONFIG_PATH, () => json({ ...CONFIG, canCreate: false, limits: { ...CONFIG.limits!, ...limits } }));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New avatar" })).toBeDisabled();
  });

  it("marks a ready avatar that can't be used right now without hiding it", async () => {
    on("GET", LIST, () =>
      json({ avatars: [READY_A, avatar({ id: ID_SAS, displayName: "Paused host", usable: false })] }),
    );
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    const paused = await waitFor(() => itemFor("Paused host"));
    expect(within(paused).getByText("Ready")).toBeInTheDocument();
    expect(within(paused).getByText("Not available to use right now.")).toBeInTheDocument();
    // Control: a usable ready avatar carries no such note.
    expect(within(itemFor("Host A")).queryByText("Not available to use right now.")).toBeNull();
  });

  it("distinguishes a gallery that failed to load from an empty one", async () => {
    on("GET", LIST, () => json({ detail: "Cosmos is unavailable." }, 503));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn't be loaded. Cosmos is unavailable.");
    expect(screen.queryByText(/No avatars yet/)).toBeNull();
    on("GET", LIST, () => json({ avatars: [] }));
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/No avatars yet/)).toBeInTheDocument();
  });
});

describe("creating an avatar", () => {
  it("keeps Create disabled until every statement is attested, then sends the server's version", async () => {
    const user = userEvent.setup();
    on("GET", LIST, () => json({ avatars: [] }));
    let posted: unknown = null;
    on("POST", LIST, (init) => {
      posted = JSON.parse(String(init?.body));
      return json(CREATED, 202);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);

    // An empty gallery opens straight onto the form.
    const create = await screen.findByRole("button", { name: "Create avatar" });
    expect(create).toBeDisabled();
    for (const key of ["Style", "Age", "Gender", "Ethnicity"]) {
      expect(screen.getByLabelText(key)).toHaveValue("");
      expect(within(screen.getByLabelText(key)).getByRole("option", { selected: true })).toHaveTextContent("Unspecified");
    }
    await fillValidDraft(user);
    await user.selectOptions(screen.getByLabelText("Style"), "Stylized3D");
    expect(create).toBeDisabled();

    const boxes = CONFIG.attestation!.statements.map((statement) =>
      screen.getByRole("checkbox", { name: statement.text }),
    );
    await user.click(boxes[0]);
    await user.click(boxes[1]);
    expect(create).toBeDisabled();
    expect(screen.getByText(/To create an avatar, confirm each statement\./)).toBeInTheDocument();
    await user.click(boxes[2]);
    expect(create).toBeEnabled();
    await user.click(boxes[1]);
    expect(create).toBeDisabled();
    await user.click(boxes[1]);
    expect(create).toBeEnabled();
    expect(screen.getByText(/Each creation is billed\. Estimated cost: \$2\.00 per avatar \(price list fixture-v1\)\./)).toBeInTheDocument();

    await user.click(create);
    await waitFor(() =>
      expect(posted).toEqual({
        displayName: "Office guide",
        prompt: "A calm host in a blue jacket",
        style: "Stylized3D",
        attestation: {
          version: "fixture-attestation-3",
          fictional: true,
          adult: true,
          notRealPerson: true,
        },
      }),
    );
    const heading = await screen.findByRole("heading", { name: "Office guide" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(within(itemFor("Office guide")).getByText("Generating…")).toBeInTheDocument();
    expect(screen.getByText(/Creating Office guide\. This can take a minute/)).toBeInTheDocument();
    expect(calls("GET", CONFIG_PATH).length).toBeGreaterThanOrEqual(2);
  });

  it("re-reads a changed attestation and sends its new version after confirming again", async () => {
    const user = userEvent.setup();
    const bodies: { attestation: { version: string } }[] = [];
    const next = {
      ...CONFIG,
      attestation: { ...CONFIG.attestation!, version: "fixture-attestation-4" },
    };
    on("POST", LIST, (init) => {
      bodies.push(JSON.parse(String(init?.body)));
      if (bodies.length === 1) {
        on("GET", CONFIG_PATH, () => json(next));
        return json({ detail: "Outdated.", code: "attestation_outdated" }, 422);
      }
      return json(CREATED, 202);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New avatar" }));
    expect(screen.getByLabelText("Name")).toHaveFocus();
    await fillValidDraft(user);
    await attestAll(user);
    await user.click(screen.getByRole("button", { name: "Create avatar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The confirmation statements changed.");
    for (const statement of CONFIG.attestation!.statements) {
      expect(screen.getByRole("checkbox", { name: statement.text })).not.toBeChecked();
    }
    expect(screen.getByRole("button", { name: "Create avatar" })).toBeDisabled();
    await attestAll(user, next);
    await user.click(screen.getByRole("button", { name: "Create avatar" }));
    await screen.findByRole("heading", { name: "Office guide" });
    expect(bodies.map((body) => body.attestation.version)).toEqual([
      "fixture-attestation-3",
      "fixture-attestation-4",
    ]);
  });

  it.each([
    ["daily_creation_limit", 429, { "Retry-After": "45" }, {}, "You've reached the creation limit for the last 24 hours. You can create another in 45 seconds."],
    ["avatar_limit_reached", 409, {}, {}, "You've reached your avatar limit. Delete an avatar to create another."],
    ["cost_unknown_under_cap", 503, {}, {}, "Creation is paused because the cost of an avatar is unknown and your usage has a spending cap."],
    ["policy_denied", 403, {}, {}, "Your organization's access policy doesn't allow this."],
    ["photo_avatars_unavailable", 503, {}, { reason: "capability_unavailable" }, "Creating avatars needs Microsoft's Limited Access approval for custom avatars, and that approval isn't in place for this deployment."],
  ])("explains a %s refusal and re-reads the limits", async (code, status, headers, extra, message) => {
    const user = userEvent.setup();
    on("POST", LIST, () => json({ detail: "Refused.", code, ...extra }, status, headers));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New avatar" }));
    await fillValidDraft(user);
    await attestAll(user);
    await user.click(screen.getByRole("button", { name: "Create avatar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    await waitFor(() => expect(calls("GET", CONFIG_PATH)).toHaveLength(2));
    expect(calls("POST", LIST)).toHaveLength(1);
    // A definite refusal (these 5xx codes are all raised before dispatch) is
    // no unknown outcome: the gallery is not re-read and the ticks stay.
    expect(calls("GET", LIST)).toHaveLength(1);
    for (const statement of CONFIG.attestation!.statements) {
      expect(screen.getByRole("checkbox", { name: statement.text })).toBeChecked();
    }
  });

  it.each([
    ["a proxy-shaped 502 with no code", 502, { detail: "API upstream unavailable" }],
    ["a service_unavailable 503", 503, { detail: "Service temporarily unavailable", code: "service_unavailable" }],
    ["an internal_error 500", 500, { detail: "Internal server error", code: "internal_error" }],
    ["a gateway_timeout 504", 504, { detail: "Gateway timeout.", code: "gateway_timeout" }],
  ])("treats %s from create as an unknown outcome and never re-sends it", async (_label, status, body) => {
    const user = userEvent.setup();
    on("POST", LIST, () => {
      // The provider accepted before the reply was lost.
      on("GET", LIST, () => json({ avatars: [CREATED, READY_A] }));
      return json(body, status);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New avatar" }));
    await fillValidDraft(user);
    await attestAll(user);
    await user.click(screen.getByRole("button", { name: "Create avatar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "it isn't known whether the avatar was created",
    );
    // The gallery is re-read, and the avatar that was created shows up.
    expect(await screen.findByRole("heading", { name: "Office guide" })).toBeInTheDocument();
    expect(calls("GET", LIST)).toHaveLength(2);
    // The same form is not immediately re-sendable: the ticks are cleared.
    for (const statement of CONFIG.attestation!.statements) {
      expect(screen.getByRole("checkbox", { name: statement.text })).not.toBeChecked();
    }
    expect(screen.getByRole("button", { name: "Create avatar" })).toBeDisabled();
    expect(calls("POST", LIST)).toHaveLength(1);
  });

  it("never repeats a create whose outcome is unknown, and re-reads the gallery instead", async () => {
    const user = userEvent.setup();
    on("POST", LIST, () => {
      on("GET", LIST, () => json({ avatars: [CREATED, READY_A] }));
      throw new TypeError("Failed to fetch");
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New avatar" }));
    await fillValidDraft(user);
    await attestAll(user);
    await user.click(screen.getByRole("button", { name: "Create avatar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("it isn't known whether the avatar was created");
    expect(await screen.findByRole("heading", { name: "Office guide" })).toBeInTheDocument();
    expect(calls("POST", LIST)).toHaveLength(1);
    expect(calls("GET", LIST)).toHaveLength(2);
  });

  it.each([
    ["changes", true],
    ["stays the same", false],
  ])("when a delete-triggered refresh's attestation version %s, ticks follow it", async (_label, changed) => {
    const user = userEvent.setup();
    const next: PhotoAvatarConfig = changed
      ? {
          ...CONFIG,
          attestation: {
            version: "fixture-attestation-9",
            statements: [
              { id: "fictional", text: "Revised statement: the character is invented by you." },
              { id: "adult", text: "Revised statement: the character is 18 or older." },
              { id: "notRealPerson", text: "Revised statement: the character is not anyone real." },
            ],
          },
        }
      : CONFIG;
    let posted: { attestation: { version: string } } | null = null;
    on("POST", LIST, (init) => {
      posted = JSON.parse(String(init?.body));
      return json(CREATED, 202);
    });
    on("DELETE", `${LIST}/${ID_A}`, () => {
      on("GET", CONFIG_PATH, () => json(next));
      return new Response(null, { status: 204 });
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New avatar" }));
    await fillValidDraft(user);
    await attestAll(user);
    const create = screen.getByRole("button", { name: "Create avatar" });
    expect(create).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Delete Host A" }));
    await user.click(screen.getByRole("button", { name: "Delete Host A permanently" }));
    await waitFor(() => expect(calls("GET", CONFIG_PATH)).toHaveLength(2));
    await screen.findByText("Deleted Host A.");

    const boxes = next.attestation!.statements.map((statement) =>
      screen.getByRole("checkbox", { name: statement.text }),
    );
    if (changed) {
      // New wording: the earlier ticks are consent to text that is gone.
      for (const box of boxes) expect(box).not.toBeChecked();
      expect(create).toBeDisabled();
      expect(screen.getByText(/To create an avatar, confirm each statement\./)).toBeInTheDocument();
      await attestAll(user, next);
      expect(create).toBeEnabled();
    } else {
      // Control: an unchanged version keeps the ticks and Create stays available.
      for (const box of boxes) expect(box).toBeChecked();
      expect(create).toBeEnabled();
    }
    await user.click(create);
    await waitFor(() => expect(posted?.attestation.version).toBe(next.attestation!.version));
  });

  it("counts the description against promptMaxChars and blocks an over-long one", async () => {
    const user = userEvent.setup();
    on("GET", CONFIG_PATH, () => json({ ...CONFIG, limits: { ...CONFIG.limits!, promptMaxChars: 10 } }));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New avatar" }));
    await user.type(screen.getByLabelText("Name"), "Guide");
    await attestAll(user);
    const description = screen.getByLabelText("Description");
    await user.type(description, "0123456789");
    expect(screen.getByText("10 / 10 characters")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create avatar" })).toBeEnabled();
    await user.type(description, "AB");
    expect(screen.getByText("12 / 10 characters · 2 over the limit")).toBeInTheDocument();
    expect(description).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Create avatar" })).toBeDisabled();
  });
});

describe("status polling", () => {
  it.each([
    ["ready", { status: "ready", preview: avatar({ id: ID_PENDING }).preview, usable: true, readyAt: LATER }, "Ready", "Pending host is ready."],
    ["failed", { status: "failed", failure: { code: "provider_rejected", message: "The avatar service did not accept this description." } }, "Couldn't create", "Couldn't create Pending host. The avatar service did not accept this description."],
  ] as const)("polls with backoff and stops once the avatar is %s", async (_label, terminal, statusText, announcement) => {
    vi.useFakeTimers();
    let reads = 0;
    on("GET", LIST, () => json({ avatars: [PENDING] }));
    on("GET", `${LIST}/${ID_PENDING}`, () => {
      reads += 1;
      return json(reads < 2 ? PENDING : { ...PENDING, ...terminal, updatedAt: LATER });
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    expect(within(itemFor("Pending host")).getByText("Generating…")).toBeInTheDocument();
    await advance(1_999);
    expect(reads).toBe(0);
    await advance(1);
    expect(reads).toBe(1);
    await advance(2_999);
    expect(reads).toBe(1);
    await advance(1);
    expect(reads).toBe(2);
    expect(within(itemFor("Pending host")).getByText(statusText)).toBeInTheDocument();
    expect(screen.getByText(announcement)).toBeInTheDocument();
    await advance(PHOTO_AVATAR_POLL_BUDGET_MS);
    expect(reads).toBe(2);
  });

  it.each([false, true])("stops polling when the panel unmounts (unmounted=%s)", async (unmount) => {
    vi.useFakeTimers();
    let reads = 0;
    on("GET", LIST, () => json({ avatars: [PENDING] }));
    on("GET", `${LIST}/${ID_PENDING}`, () => {
      reads += 1;
      return json(PENDING);
    });
    const view = render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    await advance(5_000);
    expect(reads).toBe(2);
    if (unmount) view.unmount();
    await advance(60_000);
    // Control: the same fixture left mounted keeps backing off and reading.
    if (unmount) expect(reads).toBe(2);
    else expect(reads).toBeGreaterThan(2);
  });

  it("pauses while the tab is hidden and resumes when it is visible again", async () => {
    vi.useFakeTimers();
    visibility = "hidden";
    let reads = 0;
    on("GET", LIST, () => json({ avatars: [PENDING] }));
    on("GET", `${LIST}/${ID_PENDING}`, () => {
      reads += 1;
      return json(PENDING);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    await advance(60_000);
    expect(reads).toBe(0);
    visibility = "visible";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toBe(1);
  });

  it("stops at its bounded budget and checks again only when asked", async () => {
    vi.useFakeTimers();
    let reads = 0;
    on("GET", LIST, () => json({ avatars: [PENDING] }));
    on("GET", `${LIST}/${ID_PENDING}`, () => {
      reads += 1;
      return json(PENDING);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    await advance(PHOTO_AVATAR_POLL_BUDGET_MS);
    // 2, 3, 5, 8, 10 seconds, then every 15 seconds, clipped to the budget.
    expect(reads).toBe(16);
    expect(screen.getByText(/Automatic checks have stopped/)).toBeInTheDocument();
    await advance(10 * 60_000);
    expect(reads).toBe(16);
    fireEvent.click(screen.getByRole("button", { name: "Check the status of Pending host" }));
    await advance(2_000);
    expect(reads).toBe(17);
  });

  it("drops an avatar that no longer exists", async () => {
    vi.useFakeTimers();
    on("GET", LIST, () => json({ avatars: [PENDING, READY_A] }));
    on("GET", `${LIST}/${ID_PENDING}`, () => json({ detail: "Not found.", code: "not_found" }, 404));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    expect(screen.getByRole("heading", { name: "Pending host" })).toBeInTheDocument();
    await advance(2_000);
    expect(screen.queryByRole("heading", { name: "Pending host" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Host A" })).toBeInTheDocument();
  });
});

describe("re-verification", () => {
  it("shows a calm Re-verifying state while the flag is set, keeping the preview and Delete", async () => {
    on("GET", LIST, () => json({ avatars: [REVERIFYING] }));
    on("GET", `${LIST}/${ID_REVERIFY}`, () => json(REVERIFYING));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    const item = await waitFor(() => itemFor("Checked host"));
    const status = within(item).getByText("Re-verifying…").closest("p");
    expect(status).toHaveAttribute("data-tone", "info");
    expect(within(item).getByText("Live use is paused until the avatar service confirms it again.")).toBeInTheDocument();
    // Non-alarming: no alert, no failure wording, no generic "unavailable" note.
    expect(within(item).queryByRole("alert")).toBeNull();
    expect(within(item).queryByText(/Couldn't|No longer available/)).toBeNull();
    expect(within(item).queryByText("Not available to use right now.")).toBeNull();
    // The preview, its label, Report and Delete all stay available.
    expect(await within(item).findByRole("img", { name: "Preview of Checked host" })).toBeInTheDocument();
    expect(within(item).getByText("AI-generated")).toBeInTheDocument();
    expect(within(item).getByRole("button", { name: "Delete Checked host" })).toBeEnabled();
    expect(within(item).getByRole("button", { name: "Report a problem with Checked host" })).toBeEnabled();
  });

  it.each([
    ["false", false],
    ["absent", undefined],
  ])("shows plain Ready when the flag is %s", async (_label, needsReverification) => {
    const cleared = { ...REVERIFYING, usable: true, needsReverification };
    on("GET", LIST, () => json({ avatars: [cleared] }));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    const item = await waitFor(() => itemFor("Checked host"));
    expect(within(item).getByText("Ready").closest("p")).toHaveAttribute("data-tone", "success");
    expect(within(item).queryByText("Re-verifying…")).toBeNull();
    expect(within(item).queryByText(/Live use is paused/)).toBeNull();
    expect(await within(item).findByRole("img", { name: "Preview of Checked host" })).toBeInTheDocument();
  });

  it("re-reads a flagged avatar at once and each minute until the server clears it", async () => {
    vi.useFakeTimers();
    let reads = 0;
    let unflaggedReads = 0;
    on("GET", LIST, () => json({ avatars: [REVERIFYING, READY_A] }));
    on("GET", `${LIST}/${ID_REVERIFY}`, () => {
      reads += 1;
      return json(reads < 2 ? REVERIFYING : { ...REVERIFYING, needsReverification: false, usable: true, updatedAt: LATER });
    });
    on("GET", `${LIST}/${ID_A}`, () => {
      unflaggedReads += 1;
      return json(READY_A);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    // The immediate re-read is a zero-delay timer scheduled once the list commits.
    await flush();
    // The list never re-verifies, so the flagged record is read straight away.
    expect(reads).toBe(1);
    expect(within(itemFor("Checked host")).getByText("Re-verifying…")).toBeInTheDocument();
    await advance(59_999);
    expect(reads).toBe(1);
    await advance(1);
    expect(reads).toBe(2);
    expect(within(itemFor("Checked host")).getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Checked host is verified again.")).toBeInTheDocument();
    await advance(10 * 60_000);
    expect(reads).toBe(2);
    // Control: a ready avatar without the flag is never polled at all.
    expect(unflaggedReads).toBe(0);
  });

  it("stops re-reading after its bounded budget and checks again only when asked", async () => {
    vi.useFakeTimers();
    let reads = 0;
    on("GET", LIST, () => json({ avatars: [REVERIFYING] }));
    on("GET", `${LIST}/${ID_REVERIFY}`, () => {
      reads += 1;
      return json(REVERIFYING);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    await advance(PHOTO_AVATAR_REVERIFY_BUDGET_MS);
    // At once, then every minute for six minutes.
    expect(reads).toBe(7);
    expect(screen.getByText(/Still being checked\. Automatic checks have stopped\./)).toBeInTheDocument();
    await advance(10 * 60_000);
    expect(reads).toBe(7);
    fireEvent.click(screen.getByRole("button", { name: "Check the status of Checked host" }));
    await flush();
    expect(reads).toBe(8);
  });

  it("says a ready avatar that fails re-verification is no longer available", async () => {
    vi.useFakeTimers();
    on("GET", LIST, () => json({ avatars: [REVERIFYING] }));
    on("GET", `${LIST}/${ID_REVERIFY}`, () =>
      json({
        ...REVERIFYING,
        status: "failed",
        needsReverification: false,
        preview: null,
        failure: { code: "provider_missing", message: "The avatar no longer exists in the avatar service." },
        updatedAt: LATER,
      }),
    );
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    await flush();
    const item = itemFor("Checked host");
    expect(within(item).getByText("No longer available")).toBeInTheDocument();
    expect(within(item).queryByText("Couldn't create")).toBeNull();
    expect(screen.getByText("Checked host is no longer available. The avatar no longer exists in the avatar service.")).toBeInTheDocument();
    expect(within(item).getByRole("button", { name: "Delete Checked host" })).toBeEnabled();
  });
});

describe("previews", () => {
  it("loads a preview only from the record's API route and never renders a foreign URL", async () => {
    const hostile = avatar({
      id: ID_HOSTILE,
      displayName: "Hostile host",
      preview: { ...avatar({ id: ID_HOSTILE }).preview!, url: "https://evil.example/avatar.png" },
    });
    const sas = avatar({
      id: ID_SAS,
      displayName: "Signed host",
      preview: {
        ...avatar({ id: ID_SAS }).preview!,
        url: "https://account.blob.core.windows.net/avatars/avatar.png?sv=2024-01-01&sig=fixture",
      },
    });
    on("GET", LIST, () => json({ avatars: [READY_A, hostile, sas] }));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);

    // Control: the well-formed record renders through its authenticated route.
    const image = await screen.findByRole("img", { name: "Preview of Host A" });
    expect(image).toHaveAttribute("src", "blob:photo-avatar-1");
    expect(calls("GET", previewPath(ID_A))).toHaveLength(1);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(within(itemFor("Host A")).getByText("AI-generated")).toBeInTheDocument();

    for (const name of ["Hostile host", "Signed host"]) {
      const item = itemFor(name);
      expect(within(item).queryByRole("img")).toBeNull();
      expect(within(item).getByText("Preview unavailable")).toBeInTheDocument();
      // The disclosure label is on every preview, even an unavailable one.
      expect(within(item).getByText("AI-generated")).toBeInTheDocument();
    }
    const urls = requestedUrls();
    expect(urls.filter((url) => /evil|blob\.core|sig=/.test(url))).toEqual([]);
    // The guard refuses the record; it does not quietly substitute a route.
    expect(urls).not.toContain(previewPath(ID_HOSTILE));
    expect(urls).not.toContain(previewPath(ID_SAS));
    for (const element of document.querySelectorAll("img")) {
      expect(element.getAttribute("src")).toMatch(/^blob:photo-avatar-\d+$/);
    }
  });

  it("shows the preview as unavailable when its bytes are not a PNG", async () => {
    on("GET", previewPath(ID_A), () => new Response("<html></html>", { headers: { "Content-Type": "text/html" } }));
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    expect(await within(await waitFor(() => itemFor("Host A"))).findByText("Preview unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Preview of Host A" })).toBeNull();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
});

describe("deleting an avatar", () => {
  it("asks first, then removes the avatar at once and announces it", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    let finish: (response: Response) => void = () => {};
    on("DELETE", `${LIST}/${ID_A}`, () => new Promise<Response>((resolve) => { finish = resolve; }));
    render(<PhotoAvatarsPanel onClose={onClose} />);

    await user.click(await screen.findByRole("button", { name: "Delete Host A" }));
    expect(screen.getByRole("group", { name: "Confirm deleting Host A" })).toHaveTextContent("Delete permanently?");
    expect(screen.getByRole("button", { name: "Keep" })).toHaveFocus();
    expect(calls("DELETE", `${LIST}/${ID_A}`)).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Keep" }));
    expect(screen.queryByRole("group", { name: "Confirm deleting Host A" })).toBeNull();
    expect(screen.getByRole("button", { name: "Delete Host A" })).toHaveFocus();

    // Escape backs out of the confirmation without closing the gallery.
    await user.click(screen.getByRole("button", { name: "Delete Host A" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "Confirm deleting Host A" })).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(calls("DELETE", `${LIST}/${ID_A}`)).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Delete Host A" }));
    await user.click(screen.getByRole("button", { name: "Delete Host A permanently" }));
    expect(calls("DELETE", `${LIST}/${ID_A}`)).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Host A" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Your avatars" })).toHaveFocus();
    await act(async () => finish(new Response(null, { status: 204 })));
    expect(await screen.findByText("Deleted Host A.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Host A" })).toBeNull();
  });

  it("puts the avatar back and waits out Retry-After while its creation is confirming", async () => {
    vi.useFakeTimers();
    on("DELETE", `${LIST}/${ID_A}`, () =>
      json({ detail: "Still confirming.", code: "avatar_confirming" }, 409, { "Retry-After": "7" }),
    );
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Delete Host A" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Host A permanently" }));
    await flush();
    const item = itemFor("Host A");
    expect(within(item).getByRole("alert")).toHaveTextContent(
      "This avatar's creation is still being confirmed. You can delete it in 7 seconds.",
    );
    // A wait the server asked for reads as a warning, not a failure.
    expect(within(item).getByRole("alert")).toHaveAttribute("data-tone", "warn");
    expect(within(item).getByRole("button", { name: "Delete Host A" })).toBeDisabled();
    await advance(6_999);
    expect(within(itemFor("Host A")).getByRole("button", { name: "Delete Host A" })).toBeDisabled();
    await advance(1);
    expect(within(itemFor("Host A")).getByRole("button", { name: "Delete Host A" })).toBeEnabled();
    expect(within(itemFor("Host A")).queryByRole("alert")).toBeNull();
  });

  it("keeps an unfinished provider deletion visible so it can be repeated", async () => {
    const user = userEvent.setup();
    on("DELETE", `${LIST}/${ID_A}`, () =>
      json({ detail: "Provider delete failed.", code: "provider_delete_failed" }, 502),
    );
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Delete Host A" }));
    await user.click(screen.getByRole("button", { name: "Delete Host A permanently" }));
    const item = await waitFor(() => itemFor("Host A"));
    expect(within(item).getByRole("alert")).toHaveTextContent("Delete it again to finish.");
    expect(within(item).getByRole("alert")).toHaveAttribute("data-tone", "danger");
    expect(within(item).getByText("Deletion unfinished")).toBeInTheDocument();
    expect(within(item).getByRole("button", { name: "Delete Host A" })).toBeEnabled();
  });
});

describe("reporting a problem", () => {
  it("offers the server's reasons, links to Microsoft and records the report", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    on("POST", `${LIST}/${ID_A}/reports`, (init) => {
      body = JSON.parse(String(init?.body));
      return json({ id: "report-1", avatarId: ID_A, reason: "impersonation", createdAt: NOW }, 202);
    });
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Report a problem with Host A" }));

    const dialog = screen.getByRole("dialog", { name: "Report a problem with Host A" });
    const send = within(dialog).getByRole("button", { name: "Send report" });
    const radios = within(dialog).getAllByRole("radio");
    expect(radios).toHaveLength(CONFIG.feedback!.reasons.length);
    expect(radios.some((radio) => (radio as HTMLInputElement).checked)).toBe(false);
    expect(send).toBeDisabled();
    const link = within(dialog).getByRole("link", { name: /report it to Microsoft/ });
    expect(link).toHaveAttribute("href", "https://aka.ms/reportabuse");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));

    await user.click(within(dialog).getByRole("radio", { name: "Looks like a real or identifiable person" }));
    expect(send).toBeEnabled();
    const details = within(dialog).getByLabelText("Details (optional)");
    await user.type(details, "x".repeat(41));
    expect(send).toBeDisabled();
    await user.clear(details);
    await user.type(details, "Resembles a news anchor");
    expect(send).toBeEnabled();
    await user.click(send);

    await waitFor(() => expect(body).toEqual({ reason: "impersonation", details: "Resembles a news anchor" }));
    expect(await within(dialog).findByText(/Your report was recorded/)).toBeInTheDocument();
    const done = within(dialog).getByRole("button", { name: "Done" });
    await waitFor(() => expect(done).toHaveFocus());
    await user.click(done);
    expect(screen.queryByRole("dialog", { name: "Report a problem with Host A" })).toBeNull();
    expect(within(itemFor("Host A")).getByText("You reported this avatar.")).toBeInTheDocument();
  });

  it("explains a report rate limit with its wait and keeps the form", async () => {
    const user = userEvent.setup();
    on("POST", `${LIST}/${ID_A}/reports`, () =>
      json({ detail: "Too many reports.", code: "report_limit" }, 429, { "Retry-After": "30" }),
    );
    render(<PhotoAvatarsPanel onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Report a problem with Host A" }));
    const dialog = screen.getByRole("dialog", { name: "Report a problem with Host A" });
    await user.click(within(dialog).getByRole("radio", { name: "Something else" }));
    await user.click(within(dialog).getByRole("button", { name: "Send report" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "You've sent several reports recently. You can send another in 30 seconds.",
    );
    expect(within(dialog).getByRole("button", { name: "Send report" })).toBeEnabled();
    expect(within(dialog).queryByText(/Your report was recorded/)).toBeNull();
  });
});
