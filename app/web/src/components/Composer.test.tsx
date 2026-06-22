// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Composer } from "./Composer";
import type { AgentSummary } from "@/lib/types";

// The voice recorder hook owns MediaRecorder/getUserMedia plumbing that jsdom
// doesn't implement, and it pulls in the API client transitively. Stub it with a
// deterministic "unsupported" recorder so these tests exercise only the composer's
// own logic (typing, submit, autocomplete) and never touch the network or mic.
const { mockToggle } = vi.hoisted(() => ({ mockToggle: vi.fn() }));
vi.mock("@/lib/voice", () => ({
  useVoiceRecorder: () => ({
    recording: false,
    transcribing: false,
    supported: false,
    toggle: mockToggle,
  }),
}));

afterEach(() => {
  cleanup();
  mockToggle.mockReset();
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
  });

  it("shows a Stop button while streaming and calls onStop when clicked", async () => {
    const { user, onStop } = setup({ streaming: true });
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("disables the voice record button when capture is unsupported", () => {
    setup();
    expect(
      screen.getByRole("button", { name: "Record a voice message" }),
    ).toBeDisabled();
  });
});
