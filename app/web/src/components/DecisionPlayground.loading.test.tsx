// @vitest-environment jsdom
import { webcrypto } from "node:crypto";
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InteractionRequiredAuthError, type AccountInfo, type AuthenticationResult } from "@azure/msal-browser";
import { initAuth } from "@/lib/auth";
import { AuthProvider } from "./AuthProvider";
import { DecisionPlayground } from "./DecisionPlayground";
import type { DecisionRequest } from "@/lib/decisions";

const config = {
  provider: "entra" as const,
  clientId: "00000000-0000-4000-8000-000000000001",
  tenantId: "00000000-0000-4000-8000-000000000002",
  apiScope: "api://00000000-0000-4000-8000-000000000003/access",
};
const account: AccountInfo = {
  homeAccountId: "synthetic.home", localAccountId: "synthetic-local",
  tenantId: config.tenantId, environment: "login.microsoftonline.com",
  username: "synthetic@example.test",
};
const token: AuthenticationResult = {
  authority: `https://login.microsoftonline.com/${config.tenantId}`,
  uniqueId: account.localAccountId, tenantId: account.tenantId, scopes: [config.apiScope],
  account, idToken: "", idTokenClaims: {}, accessToken: "synthetic-api-token",
  fromCache: true, expiresOn: new Date(Date.now() + 3600000), tokenType: "Bearer",
  correlationId: "synthetic-request",
};
const options = { models: [{ id: "Microsoft-Decision-1", displayName: "Microsoft Decision 1", residencies: ["us"] }] };
const fetchMock = vi.fn<typeof fetch>();
const client = () => {
  const value = initAuth(config);
  if (!value) throw new Error("Real MSAL client was not constructed.");
  return value;
};

beforeEach(async () => {
  vi.stubGlobal("crypto", webcrypto);
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  const msal = client();
  await msal.initialize();
  vi.spyOn(msal, "getActiveAccount").mockReturnValue(account);
  vi.spyOn(msal, "getAllAccounts").mockReturnValue([account]);
  vi.spyOn(msal, "acquireTokenSilent").mockResolvedValue(token);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mount(strict = false) {
  const element = <AuthProvider config={config}><DecisionPlayground /></AuthProvider>;
  return render(strict ? <StrictMode>{element}</StrictMode> : element);
}
async function settle() {
  await act(async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); });
}

function result(request: DecisionRequest, probability = .75) {
  return {
    model: request.model, reportedModel: "microsoft-decision-1", region: "eastus2",
    residency: "us", latencyMs: 125, costKnown: false, estimatedCostUsd: null,
    usage: { known: true, inputTokens: 10, outputTokens: 2, totalTokens: 12 },
    answers: Object.fromEntries(request.questions.map((q) => [q.name, q.type === "noul"
      ? { type: "noul", noul: probability }
      : q.type === "choice" ? {
        type: "choice", choice: Object.keys(q.criteria)[0],
        probabilities: Object.fromEntries(Object.keys(q.criteria).map((key, i) => [key, i === 0 ? 1 : 0])),
      } : { type: "score", score: 0, probabilities: { "0": 1, "1": 0, "2": 0 } }])),
  };
}
async function loadExample() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Load synthetic example" }));
}
async function cancelRun() {
  fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled());
  expect(screen.getByLabelText("Content to evaluate")).toBeEnabled();
  expect(screen.getByText("Cancelled. Already accepted work may still be billed.")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Results" })).not.toBeInTheDocument();
}

describe("Decision cancellation through the real Entra boundary", () => {
  it.each(["resolve", "reject"] as const)("unlocks before silent acquisition settles, then ignores its late %s", async (completion) => {
    fetchMock.mockImplementation(async (path, init) => path === "/api/decisions/options"
      ? Response.json(options) : Response.json(result(JSON.parse(String(init?.body)))));
    const redirect = vi.spyOn(client(), "acquireTokenRedirect").mockRejectedValue(new Error("Unexpected redirect"));
    mount();
    await loadExample();
    let resolveToken: (value: AuthenticationResult) => void = () => {};
    let rejectToken: (reason: Error) => void = () => {};
    vi.mocked(client().acquireTokenSilent).mockImplementationOnce(() => new Promise((resolve, reject) => {
      resolveToken = resolve; rejectToken = reject;
    }));
    const form = screen.getByRole("form", { name: "Decision input" });
    fireEvent.submit(form); fireEvent.submit(form);
    await settle();
    expect(client().acquireTokenSilent).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await cancelRun();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    expect(await screen.findByText("Yes probability: 75.0%")).toBeInTheDocument();
    await act(async () => {
      if (completion === "resolve") resolveToken(token);
      else rejectToken(new InteractionRequiredAuthError("interaction_required", "Synthetic renewal required"));
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(client().acquireTokenSilent).toHaveBeenCalledTimes(3);
    expect(redirect).not.toHaveBeenCalled();
    expect(screen.getByText("Yes probability: 75.0%")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled();
  });

  it.each([
    ["fetch", "resolve"], ["fetch", "reject"], ["body", "resolve"], ["body", "reject"],
  ] as const)("cancels an abort-ignoring %s and ignores its late %s after an explicit result", async (boundary, completion) => {
    let complete: (response: Response) => void = () => {};
    let finishBody: (value: unknown) => void = () => {};
    let rejectOld: (reason: Error) => void = () => {};
    let bodyStarted = false;
    let oldRequest: DecisionRequest | undefined;
    let oldSignal: AbortSignal | null | undefined;
    fetchMock.mockResolvedValueOnce(Response.json(options)).mockImplementationOnce((_path, init) => {
      oldSignal = init?.signal;
      oldRequest = JSON.parse(String(init?.body));
      if (boundary === "fetch") return new Promise((resolve, reject) => { complete = resolve; rejectOld = reject; });
      const response = Response.json({});
      vi.spyOn(response, "json").mockImplementation(() => {
        bodyStarted = true;
        return new Promise((resolve, reject) => { finishBody = resolve; rejectOld = reject; });
      });
      return Promise.resolve(response);
    }).mockImplementation(async (_path, init) => Response.json(result(JSON.parse(String(init?.body)))));
    mount();
    await loadExample();
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(bodyStarted).toBe(boundary === "body");
    if (!oldSignal || !oldRequest) throw new Error("Run did not reach fetch.");
    const request = oldRequest;
    const removeListener = vi.spyOn(oldSignal, "removeEventListener");
    await cancelRun();
    expect(oldSignal.aborted).toBe(true);
    expect(removeListener).toHaveBeenCalledWith("abort", expect.any(Function));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    expect(await screen.findByText("Yes probability: 75.0%")).toBeInTheDocument();
    await act(async () => {
      const late = result(request, .25);
      if (completion === "reject") rejectOld(new Error("Late synthetic failure"));
      else if (boundary === "fetch") complete(Response.json(late));
      else finishBody(late);
    });
    expect(screen.getByText("Yes probability: 75.0%")).toBeInTheDocument();
    expect(screen.queryByText("Yes probability: 25.0%")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled();
  });

  it("aborts an unmounted run before late interaction-required acquisition without redirect or POST", async () => {
    fetchMock.mockImplementation(async () => Response.json(options));
    const redirect = vi.spyOn(client(), "acquireTokenRedirect").mockRejectedValue(new Error("Unexpected redirect"));
    const view = mount();
    await loadExample();
    let rejectToken: (reason: Error) => void = () => {};
    vi.mocked(client().acquireTokenSilent).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectToken = reject; }));
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    await settle();
    view.unmount();
    await act(async () => { rejectToken(new InteractionRequiredAuthError("interaction_required", "Synthetic renewal required")); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(redirect).not.toHaveBeenCalled();
    mount();
    await loadExample();
    expect(screen.queryByText(/Cancelled/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("Decision availability through the real Entra boundary", () => {
  it("ends a stalled authenticated options request and allows one explicit reload", async () => {
    vi.useFakeTimers();
    let pendingSignal: AbortSignal | null | undefined;
    fetchMock.mockImplementationOnce((_url, init) => {
      pendingSignal = init?.signal;
      return new Promise(() => {});
    }).mockImplementation(async () => Response.json(options));
    mount();
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Authorization")).toBe("Bearer synthetic-api-token");
    expect(screen.getByText("Loading Decision availability...")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Decision availability timed out");
    expect(pendingSignal?.aborted).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Reload availability" }));
    await settle();
    expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("bounds token acquisition too and never sends a late aborted request", async () => {
    vi.useFakeTimers();
    let resolveToken: (result: AuthenticationResult) => void = () => {};
    vi.mocked(client().acquireTokenSilent).mockImplementationOnce(() => new Promise((resolve) => { resolveToken = resolve; }));
    fetchMock.mockImplementation(async () => Response.json(options));
    mount();
    await settle();
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Decision availability timed out");
    await act(async () => { resolveToken(token); });
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reload availability" }));
    await settle();
    expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("survives StrictMode effect cleanup and a genuine MSAL active-account event", async () => {
    fetchMock.mockImplementation(async () => Response.json(options));
    mount(true);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Content to evaluate"), { target: { value: "Keep my draft" } });
    const count = fetchMock.mock.calls.length;
    await act(async () => { client().setActiveAccount(account); });
    expect(screen.getByLabelText("Content to evaluate")).toHaveValue("Keep my draft");
    expect(fetchMock).toHaveBeenCalledTimes(count);
  });

  it("bounds a stalled response body, not only the fetch headers", async () => {
    vi.useFakeTimers();
    const body = new ReadableStream<Uint8Array>({ start() {} });
    fetchMock.mockResolvedValue(new Response(body, { headers: { "Content-Type": "application/json" } }));
    mount();
    await settle();
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Decision availability timed out");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([403, 503])("keeps a server HTTP %s distinct from an empty model catalog", async (status) => {
    fetchMock.mockImplementation(async () => new Response("<html>upstream error</html>", { status }));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent(`HTTP ${status}`);
    expect(screen.queryByText(/No Decision model is available/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run decision" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows a valid empty catalog as unavailable, not an error", async () => {
    fetchMock.mockImplementation(async () => Response.json({ models: [] }));
    mount();
    expect(await screen.findByText(/No Decision model is available/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("ignores stale timed-out completion after a successful explicit reload", async () => {
    vi.useFakeTimers();
    let complete: (response: Response) => void = () => {};
    fetchMock.mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }))
      .mockImplementation(async () => Response.json(options));
    mount();
    await settle();
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    fireEvent.click(screen.getByRole("button", { name: "Reload availability" }));
    await settle();
    fireEvent.change(screen.getByLabelText("Content to evaluate"), { target: { value: "Keep this input" } });
    await act(async () => { complete(Response.json({ models: [] })); });
    expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled();
    expect(screen.getByLabelText("Content to evaluate")).toHaveValue("Keep this input");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("unmounts during silent acquisition without a late fetch or interactive redirect, then remounts", async () => {
    let rejectToken: (error: Error) => void = () => {};
    vi.mocked(client().acquireTokenSilent).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectToken = reject; }));
    const redirect = vi.spyOn(client(), "acquireTokenRedirect").mockRejectedValue(new Error("Unexpected redirect"));
    fetchMock.mockImplementation(async () => Response.json(options));
    const first = mount();
    await settle();
    first.unmount();
    await act(async () => { rejectToken(new InteractionRequiredAuthError("interaction_required", "Synthetic renewal required")); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(redirect).not.toHaveBeenCalled();
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the first signed-out mount gated until the real MSAL account event", async () => {
    vi.mocked(client().getActiveAccount).mockReturnValue(null);
    vi.mocked(client().getAllAccounts).mockReturnValue([]);
    fetchMock.mockImplementation(async () => Response.json(options));
    mount();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    vi.mocked(client().getActiveAccount).mockReturnValue(account);
    vi.mocked(client().getAllAccounts).mockReturnValue([account]);
    await act(async () => { client().setActiveAccount(account); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
