// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Composer } from "./Composer";
import type { AgentSummary } from "@/lib/types";

afterEach(() => {
  cleanup();
});

const AGENTS: AgentSummary[] = [
  {
    name: "researcher",
    displayName: "Researcher",
    description: "Digs through the library",
    enabled: true,
  },
  {
    name: "archivist",
    displayName: "Archivist",
    description: "Disabled persona that should never appear",
    enabled: false,
  },
];

type ComposerProps = Parameters<typeof Composer>[0];

function setup(overrides: Partial<ComposerProps> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const props: ComposerProps = {
    disabled: false,
    streaming: false,
    agents: [],
    documents: [],
    libraryDocuments: [],
    uploading: false,
    capabilities: {
      ingestPath: "library",
      maxBytes: 1_000_000,
      maxPerUserDocuments: 100,
      maxPerSessionDocuments: 8,
      extensions: [".pdf", ".mp3"],
      mimeTypes: ["application/pdf", "audio/*"],
      modalities: ["document", "audio"],
    },
    onSend,
    onStop,
    onUpload: vi.fn(),
    onRemoveDocument: vi.fn(),
    ...overrides,
  };
  const user = userEvent.setup();
  render(<Composer {...props} />);
  const textarea = screen.getByRole("combobox", {
    name: "Message",
  }) as HTMLTextAreaElement;
  return { user, onSend, onStop, textarea };
}

describe("Composer", () => {
  it("renders the message input with Send disabled while empty", () => {
    setup();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("enables Send on input and calls onSend with trimmed text, then clears", async () => {
    const { user, onSend, textarea } = setup();
    const send = screen.getByRole("button", { name: "Send" });

    await user.type(textarea, "  hello world  ");
    expect(send).toBeEnabled();

    await user.click(send);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("hello world");
    expect(textarea.value).toBe("");
  });

  it("submits when Enter is pressed without Shift", async () => {
    const { user, onSend, textarea } = setup();
    await user.type(textarea, "ping{Enter}");
    expect(onSend).toHaveBeenCalledWith("ping");
  });

  it("inserts a newline on Shift+Enter instead of submitting", async () => {
    const { user, onSend, textarea } = setup();
    await user.type(textarea, "line one");
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toContain("\n");
  });

  it("does not submit Enter while an IME composition is active", async () => {
    const { onSend, textarea } = setup();
    fireEvent.change(textarea, { target: { value: "変換中" } });
    fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("変換中");
  });

  it("autosizes from the 64px minimum to an eight-line cap before scrolling", () => {
    const { textarea } = setup();
    let scrollHeight = 40;
    Object.defineProperty(textarea, "scrollHeight", {
      configurable: true,
      get: () => scrollHeight,
    });

    fireEvent.change(textarea, { target: { value: "short" } });
    expect(textarea.style.height).toBe("64px");
    expect(textarea.style.overflowY).toBe("hidden");

    scrollHeight = 400;
    fireEvent.change(textarea, {
      target: { value: Array.from({ length: 12 }, (_, index) => `line ${index}`).join("\n") },
    });
    expect(Number.parseFloat(textarea.style.height)).toBeLessThan(400);
    expect(textarea.style.overflowY).toBe("auto");

    scrollHeight = 0;
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(textarea.style.height).toBe("64px");
    expect(textarea.style.overflowY).toBe("hidden");
  });

  it("keeps Send disabled and never submits whitespace-only input", async () => {
    const { user, onSend, textarea } = setup();
    await user.type(textarea, "    ");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.type(textarea, "{Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("keeps Send disabled and blocks submit when the composer is disabled", async () => {
    const { user, onSend, textarea } = setup({ disabled: true });
    await user.type(textarea, "should not send{Enter}");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("opens the agent mention menu filtered to enabled agents when typing @", async () => {
    const { user, textarea } = setup({ agents: AGENTS });
    expect(screen.queryByRole("listbox")).toBeNull();

    await user.type(textarea, "@");

    expect(screen.getByRole("listbox", { name: "Agents" })).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: /researcher/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /archivist/i })).toBeNull();
  });

  it("inserts the chosen agent mention into the input when an option is clicked", async () => {
    const { user, onSend, textarea } = setup({ agents: AGENTS });
    await user.type(textarea, "@res");
    await user.click(screen.getByRole("option", { name: /researcher/i }));
    expect(textarea.value).toBe("@researcher ");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("opens the slash command menu when typing /", async () => {
    const { user, textarea } = setup();
    await user.type(textarea, "/");
    expect(screen.getByRole("listbox", { name: "Commands" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /help/i })).toBeInTheDocument();

    await user.type(textarea, "res");
    expect(screen.getByRole("option", { name: /research/i })).toBeInTheDocument();
  });

  it("shows a Stop button while streaming and calls onStop when clicked", async () => {
    const { user, onStop } = setup({ streaming: true });
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("uses one microphone action for Voice Live and no dictation/settings controls", () => {
    setup({
      voiceLive: {
        active: false,
        supported: true,
        connecting: false,
        ending: false,
        saving: false,
        saveBlocked: false,
        retrying: false,
        start: vi.fn(),
        stop: vi.fn(),
      },
    });
    expect(screen.getByRole("button", { name: "Start live voice conversation" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Record a voice message" })).toBeNull();
    expect(screen.queryByText("Voice settings")).toBeNull();
  });

  // Regression: starting Voice Live could leave a user stuck in the chat with
  // no way to stop it, because the button's disabled condition OR'd in
  // saving/saveBlocked from a *previous* cycle regardless of whether the
  // *current* cycle was actively connecting/live. Stop must always be
  // reachable while active, no matter what a prior cycle's save state is.
  it("keeps Stop enabled and correctly labeled while connecting even if a previous cycle's save is stuck", async () => {
    const stop = vi.fn();
    const { user } = setup({
      voiceLive: {
        active: true,
        supported: true,
        connecting: true,
        ending: false,
        saving: true,
        saveBlocked: true,
        retrying: false,
        start: vi.fn(),
        stop,
      },
    });
    const button = screen.getByRole("button", { name: "Stop live voice conversation" });
    expect(button).toBeEnabled();
    await user.click(button);
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("keeps Stop enabled while live even if a previous cycle's save is stuck", async () => {
    const stop = vi.fn();
    setup({
      voiceLive: {
        active: true,
        supported: true,
        connecting: false,
        ending: false,
        saving: true,
        saveBlocked: true,
        retrying: false,
        start: vi.fn(),
        stop,
      },
    });
    expect(screen.getByRole("button", { name: "Stop live voice conversation" })).toBeEnabled();
  });

  it("disables the mic action for a blocked/saving previous cycle only while not active", () => {
    setup({
      voiceLive: {
        active: false,
        supported: true,
        connecting: false,
        ending: false,
        saving: false,
        saveBlocked: true,
        retrying: false,
        start: vi.fn(),
        stop: vi.fn(),
      },
    });
    expect(
      screen.getByRole("button", { name: "Retry saving the voice transcript below" }),
    ).toBeDisabled();
  });

  it("uploads multiple selected files sequentially", async () => {
      let releaseFirst!: () => void;
      const first = new Promise<void>((resolve) => {
        releaseFirst = resolve;
      });
      const onUpload = vi
        .fn<(file: File) => Promise<void>>()
        .mockImplementationOnce(() => first)
        .mockResolvedValue(undefined);
      const { user } = setup({ onUpload });
      const input = document.querySelector('input[type="file"]') as HTMLInputElement;
      const files = [
        new File(["one"], "one.pdf", { type: "application/pdf" }),
        new File(["two"], "two.mp3", { type: "audio/mpeg" }),
      ];
      const uploadPromise = user.upload(input, files);
      await waitFor(() => expect(onUpload).toHaveBeenCalledTimes(1));
      releaseFirst();
      await uploadPromise;
      expect(onUpload).toHaveBeenCalledTimes(2);
      expect(onUpload.mock.calls.map(([file]) => file.name)).toEqual([
        "one.pdf",
        "two.mp3",
      ]);
  });

  it("uses server capabilities for client feedback without replacing API authority", async () => {
      const onUpload = vi.fn(async () => {});
      const onError = vi.fn();
      const { user } = setup({
        onUpload,
        onError,
        capabilities: {
          ingestPath: "session",
          maxBytes: 3,
          maxPerUserDocuments: null,
          maxPerSessionDocuments: 8,
          extensions: [".txt"],
          mimeTypes: ["text/*"],
          modalities: ["text"],
        },
      });

      const input = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(
        input,
        new File(["oversize"], "large.txt", { type: "text/plain" }),
      );
      expect(onUpload).not.toHaveBeenCalled();
      expect(onError).toHaveBeenCalledWith(expect.stringContaining("exceeds"));
  });

  it("disables Attach until capabilities load and offers an explicit retry", async () => {
    const onRetryCapabilities = vi.fn();
    setup({
      capabilities: null,
      capabilitiesError: "configuration unavailable",
      onRetryCapabilities,
    });
    expect(screen.getByRole("button", { name: "Attachments unavailable" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("configuration unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetryCapabilities).toHaveBeenCalled();
  });

  it("labels retry and dismiss actions with the failed filename", async () => {
      const onRetryUpload = vi.fn();
      const onDismissUpload = vi.fn();
      const { user } = setup({
        uploads: [
          {
            id: "u1",
            filename: "meeting.mp3",
            status: "failed",
            error: "network error",
          },
        ],
        onRetryUpload,
        onDismissUpload,
      });
      await user.click(
        screen.getByRole("button", { name: "Retry upload meeting.mp3" }),
      );
      expect(onRetryUpload).toHaveBeenCalledWith("u1");
      await user.click(
        screen.getByRole("button", {
          name: "Dismiss failed upload meeting.mp3",
        }),
      );
      expect(onDismissUpload).toHaveBeenCalledWith("u1");
  });
});
