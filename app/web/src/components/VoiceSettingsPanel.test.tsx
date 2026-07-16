// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { VoiceSettingsPanel, type VoiceSettingsPanelProps } from "./VoiceSettingsPanel";
import { DEFAULT_VOICE_SETTINGS } from "@/lib/voiceLive";
import type { AgentSummary } from "@/lib/types";

afterEach(() => {
  cleanup();
});

const AGENTS: AgentSummary[] = [
  { name: "analyst", displayName: "Analyst", description: "", enabled: true },
  { name: "writer", displayName: "Writer", description: "", enabled: true },
  { name: "retired", displayName: "Retired", description: "", enabled: false },
];

const MODELS = [
  { id: "gpt-realtime", displayName: "GPT Realtime" },
  { id: "gpt-realtime-mini", displayName: "GPT Realtime Mini" },
];

function setup(overrides: Partial<VoiceSettingsPanelProps> = {}) {
  const onAgentChange = vi.fn();
  const onModelChange = vi.fn();
  const onVoiceChange = vi.fn();
  const onToolsChange = vi.fn();
  const onSettingsChange = vi.fn();
  const onReset = vi.fn();
  const props: VoiceSettingsPanelProps = {
    agents: AGENTS,
    defaultAgentLabel: "Current chat agent",
    explicitAgent: null,
    onAgentChange,
    models: MODELS,
    defaultModelLabel: "Default (GPT Realtime)",
    explicitModel: null,
    onModelChange,
    voice: "alloy",
    onVoiceChange,
    toolsAvailable: true,
    tools: false,
    onToolsChange,
    settings: DEFAULT_VOICE_SETTINGS,
    onSettingsChange,
    onReset,
    locked: false,
    ...overrides,
  };
  const user = userEvent.setup();
  render(<VoiceSettingsPanel {...props} />);
  return {
    user,
    onAgentChange,
    onModelChange,
    onVoiceChange,
    onToolsChange,
    onSettingsChange,
    onReset,
  };
}

describe("VoiceSettingsPanel", () => {
  it("renders as a disclosure, not a dialog, and is closed by default", () => {
    setup();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("Voice settings")).toBeInTheDocument();
    // The agent select exists in the DOM (native <details> content is always
    // present; only its visibility toggles) but the summary is what the user
    // sees collapsed.
    expect(screen.getByRole("combobox", { name: "Agent" })).toBeInTheDocument();
  });

  it("only lists enabled agents plus the default option", async () => {
    setup();
    const select = screen.getByRole("combobox", { name: "Agent" });
    const options = within(select).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      "Current chat agent",
      "Analyst",
      "Writer",
    ]);
  });

  it("only lists realtime catalog models plus the default option", () => {
    setup();
    const select = screen.getByRole("combobox", { name: "Realtime model" });
    const options = within(select).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      "Default (GPT Realtime)",
      "GPT Realtime",
      "GPT Realtime Mini",
    ]);
  });

  it("lists every REALTIME_VOICES entry in the voice select", () => {
    setup();
    const select = screen.getByRole("combobox", { name: "Voice" });
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(
      expect.arrayContaining(["alloy", "marin", "cedar", "shimmer"]),
    );
  });

  it("calls onAgentChange with null for the default option and the name otherwise", async () => {
    const { user, onAgentChange } = setup({ explicitAgent: "analyst" });
    const select = screen.getByRole("combobox", { name: "Agent" });
    await user.selectOptions(select, "Writer");
    expect(onAgentChange).toHaveBeenCalledWith("writer");

    await user.selectOptions(select, "Current chat agent");
    expect(onAgentChange).toHaveBeenCalledWith(null);
  });

  it("only shows the governed-tools opt-in when the server advertises it", () => {
    setup({ toolsAvailable: false });
    expect(screen.queryByRole("checkbox")).toBeNull();
    cleanup();
    setup({ toolsAvailable: true });
    expect(
      screen.getByRole("checkbox", { name: "Allow governed tools in voice" }),
    ).toBeInTheDocument();
  });

  it("exposes advanced instructions, temperature, VAD, transcription, and language controls", () => {
    setup();
    expect(screen.getByRole("textbox", { name: "Instructions" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Temperature" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Turn detection" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "VAD threshold" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Silence (ms)" })).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: "Transcription model" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Language hint" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset defaults" })).toBeInTheDocument();
  });

  it("calls onReset when Reset defaults is clicked", async () => {
    const { user, onReset } = setup();
    await user.click(screen.getByRole("button", { name: "Reset defaults" }));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("disables every control while locked but keeps them visible", () => {
    setup({ locked: true });
    expect(screen.getByRole("combobox", { name: "Agent" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Realtime model" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Voice" })).toBeDisabled();
    expect(
      screen.getByRole("checkbox", { name: "Allow governed tools in voice" }),
    ).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Instructions" })).toBeDisabled();
    expect(screen.getByRole("spinbutton", { name: "Temperature" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reset defaults" })).toBeDisabled();
  });

  it("enables edits while idle, applying to the next connection only", async () => {
    const { user, onVoiceChange } = setup({ locked: false });
    const select = screen.getByRole("combobox", { name: "Voice" });
    expect(select).toBeEnabled();
    await user.selectOptions(select, "marin");
    expect(onVoiceChange).toHaveBeenCalledWith("marin");
  });
});
