// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { hardenAvatarVideoElement } from "./avatarVideo";

afterEach(() => {
  vi.restoreAllMocks();
  Reflect.deleteProperty(document, "pictureInPictureElement");
  Reflect.deleteProperty(document, "exitPictureInPicture");
  Reflect.deleteProperty(document, "fullscreenElement");
  Reflect.deleteProperty(document, "exitFullscreen");
});

function stubDocument(field: "pictureInPictureElement" | "fullscreenElement", value: Element | null) {
  Object.defineProperty(document, field, { configurable: true, get: () => value });
}

describe("hardenAvatarVideoElement", () => {
  it("keeps the avatar video inside its labelled stage", () => {
    const video = document.createElement("video");
    hardenAvatarVideoElement(video);
    expect(video).toHaveAttribute("disablepictureinpicture");
    expect(video).toHaveAttribute("disableremoteplayback");
    expect(video.getAttribute("controlslist")).toContain("nofullscreen");
    expect(video.getAttribute("controlslist")).toContain("noremoteplayback");
    expect(video.controls).toBe(false);
    const menu = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    video.dispatchEvent(menu);
    expect(menu.defaultPrevented).toBe(true);
    // Control: an unhardened video leaves its context menu alone.
    const plain = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    document.createElement("video").dispatchEvent(plain);
    expect(plain.defaultPrevented).toBe(false);
  });

  it("leaves picture-in-picture at once if the browser enters it anyway", () => {
    const video = document.createElement("video");
    hardenAvatarVideoElement(video);
    const exitPictureInPicture = vi.fn(() => Promise.resolve());
    Object.defineProperty(document, "exitPictureInPicture", { configurable: true, value: exitPictureInPicture });
    // Control: another element in picture-in-picture is not this avatar's concern.
    stubDocument("pictureInPictureElement", document.createElement("video"));
    video.dispatchEvent(new Event("enterpictureinpicture"));
    expect(exitPictureInPicture).not.toHaveBeenCalled();
    stubDocument("pictureInPictureElement", video);
    video.dispatchEvent(new Event("enterpictureinpicture"));
    expect(exitPictureInPicture).toHaveBeenCalledTimes(1);
  });

  it("leaves fullscreen at once if the avatar video enters it anyway", () => {
    const video = document.createElement("video");
    hardenAvatarVideoElement(video);
    const exitFullscreen = vi.fn(() => Promise.resolve());
    Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exitFullscreen });
    // Control: leaving fullscreen (no fullscreen element) needs no exit.
    stubDocument("fullscreenElement", null);
    video.dispatchEvent(new Event("fullscreenchange"));
    expect(exitFullscreen).not.toHaveBeenCalled();
    stubDocument("fullscreenElement", video);
    video.dispatchEvent(new Event("fullscreenchange"));
    expect(exitFullscreen).toHaveBeenCalledTimes(1);
  });

  it("tolerates engines without a promise-returning exit", () => {
    const video = document.createElement("video");
    hardenAvatarVideoElement(video);
    // jsdom reports a listener's exception to window "error" rather than
    // throwing it from dispatchEvent, so the test listens there.
    const escaped: unknown[] = [];
    const onError = (event: ErrorEvent) => {
      escaped.push(event.error);
      event.preventDefault();
    };
    window.addEventListener("error", onError);
    try {
      // Control: a listener that throws is observed.
      const control = document.createElement("video");
      control.addEventListener("fullscreenchange", () => {
        throw new Error("control");
      });
      control.dispatchEvent(new Event("fullscreenchange"));
      expect(escaped).toHaveLength(1);
      const exitFullscreen = vi.fn(() => {
        throw new Error("not supported");
      });
      Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exitFullscreen });
      stubDocument("fullscreenElement", video);
      video.dispatchEvent(new Event("fullscreenchange"));
      expect(exitFullscreen).toHaveBeenCalledTimes(1);
      expect(escaped).toHaveLength(1);
    } finally {
      window.removeEventListener("error", onError);
    }
  });
});
