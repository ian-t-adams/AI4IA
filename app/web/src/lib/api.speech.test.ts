import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the same-origin proxy so synthesizeSpeech can be driven without a network.
vi.mock("./auth", () => ({ apiFetch: vi.fn() }));

import { synthesizeSpeech } from "./api";
import { apiFetch } from "./auth";

const mockApiFetch = vi.mocked(apiFetch);

function speechResponse(opts: {
  ok?: boolean;
  status?: number;
  statusText?: string;
  contentType?: string | null;
  blob?: Blob;
  json?: () => Promise<unknown>;
}): Response {
  const { ok = true, status = 200, statusText = "OK", contentType = "audio/mpeg" } = opts;
  const blob = opts.blob ?? new Blob(["fake-audio-bytes"], { type: contentType ?? undefined });
  return {
    ok,
    status,
    statusText,
    headers: {
      get: (name: string) => (name.toLowerCase() === "content-type" ? contentType : null),
    },
    blob: async () => blob,
    json: opts.json ?? (async () => ({})),
  } as unknown as Response;
}

afterEach(() => mockApiFetch.mockReset());

describe("synthesizeSpeech", () => {
  it("returns the audio blob for a well-formed audio response", async () => {
    const blob = new Blob(["abc"], { type: "audio/mpeg" });
    mockApiFetch.mockResolvedValue(speechResponse({ contentType: "audio/mpeg", blob }));
    await expect(synthesizeSpeech("hello")).resolves.toBe(blob);
  });

  it("accepts a content type with charset/codec parameters", async () => {
    const blob = new Blob(["abc"], { type: "audio/webm" });
    mockApiFetch.mockResolvedValue(
      speechResponse({ contentType: "audio/webm; codecs=opus", blob }),
    );
    await expect(synthesizeSpeech("hello")).resolves.toBe(blob);
  });

  // Regression: a misconfigured gateway hop or auth interstitial can return
  // `ok: true` with an HTML/JSON body instead of audio. Previously this blob
  // was handed straight to an <audio> element, which failed with the opaque
  // "Couldn't play the synthesized audio." error with no indication the
  // fetched payload was never audio in the first place.
  it("rejects a non-audio content type instead of returning an unplayable blob", async () => {
    mockApiFetch.mockResolvedValue(
      speechResponse({ contentType: "text/html", blob: new Blob(["<html>error</html>"]) }),
    );
    await expect(synthesizeSpeech("hello")).rejects.toThrow(
      "Speech synthesis returned text/html instead of audio. Try again.",
    );
  });

  it("rejects a missing content type", async () => {
    mockApiFetch.mockResolvedValue(speechResponse({ contentType: null }));
    await expect(synthesizeSpeech("hello")).rejects.toThrow(
      "Speech synthesis returned an unrecognized response. Try again.",
    );
  });

  it("rejects an empty audio body even when the content type is correct", async () => {
    mockApiFetch.mockResolvedValue(
      speechResponse({ contentType: "audio/mpeg", blob: new Blob([], { type: "audio/mpeg" }) }),
    );
    await expect(synthesizeSpeech("hello")).rejects.toThrow(
      "Speech synthesis returned an empty audio clip. Try again.",
    );
  });

  it("surfaces the server error detail on a non-OK response", async () => {
    mockApiFetch.mockResolvedValue(
      speechResponse({
        ok: false,
        status: 502,
        statusText: "Bad Gateway",
        json: async () => ({ detail: "Speech synthesis failed." }),
      }),
    );
    await expect(synthesizeSpeech("hello")).rejects.toThrow("502: Speech synthesis failed.");
  });
});
