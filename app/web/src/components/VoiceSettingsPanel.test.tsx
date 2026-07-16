// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { VoiceSettingsPanel, type VoiceSettingsPanelProps } from "./VoiceSettingsPanel";
import {
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_VOICE_SETTINGS,
} from "@/lib/voiceLive";
import { voiceProviderCatalog } from "@/lib/data/voice_provider_catalog";
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

const PROVIDERS = voiceProviderCatalog.providers.map(
  (provider: (typeof voiceProviderCatalog.providers)[number]) => ({
  id: provider.id,
  displayLabel: provider.displayLabel,
  description: provider.description,
  }),
);

function setup(overrides: Partial<VoiceSettingsPanelProps> = {}) {
  const onAgentChange = vi.fn();
  const onProviderChange = vi.fn();
  const onModelChange = vi.fn();
  const onVoiceChange = vi.fn();
  const onSpeechModelChange = vi.fn();
  const onToolsChange = vi.fn();
  const onSettingsChange = vi.fn();
  const onSpeechSettingsChange = vi.fn();
  const onReset = vi.fn();
  const props: VoiceSettingsPanelProps = {
    agents: AGENTS,
    providers: PROVIDERS,
    provider: "azure_openai",
    onProviderChange,
    activeProvider: voiceProviderCatalog.providers[0],
    defaultAgentLabel: "Current chat agent",
    explicitAgent: null,
    onAgentChange,
    models: MODELS,
    defaultModelLabel: "Default (GPT Realtime)",
    explicitModel: null,
    onModelChange,
    speechModel: "gpt-realtime",
    onSpeechModelChange,
    voice: "alloy",
    onVoiceChange,
    toolsAvailable: true,
    tools: false,
    onToolsChange,
    settings: DEFAULT_VOICE_SETTINGS,
    onSettingsChange,
    speechSettings: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
    onSpeechSettingsChange,
    onReset,
    locked: false,
    ...overrides,
  };
  const user = userEvent.setup();
  render(<VoiceSettingsPanel {...props} />);
  return {
    user,
    onAgentChange,
    onProviderChange,
    onModelChange,
    onVoiceChange,
    onSpeechModelChange,
    onToolsChange,
    onSettingsChange,
    onSpeechSettingsChange,
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

  it("offers the server-advertised providers and defaults to Azure OpenAI", () => {
    setup();
    const select = screen.getByRole("combobox", { name: "Provider" });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Azure OpenAI",
      "Azure Speech",
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

  it("shows speech-specific controls when Azure Speech is selected", () => {
    setup({
      provider: "speech_voice_live",
      activeProvider: voiceProviderCatalog.providers[1],
      voice: voiceProviderCatalog.providers[1].capabilities.voices.default,
      speechSettings: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
    });
    expect(screen.getByRole("combobox", { name: "Locale" })).toBeInTheDocument();
    const model = screen.getByRole("combobox", { name: "Speech model" });
    expect(within(model).getAllByRole("option")).toHaveLength(6);
    expect(screen.queryByRole("combobox", { name: "Transcription" })).toBeNull();
    expect(screen.getByText(/Native audio · GPT-4o Transcribe · eastus2/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Turn detection" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Noise suppression" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Echo cancellation" })).toBeInTheDocument();
  });

  it("selects a Speech model and shows its catalog profile without changing OpenAI", async () => {
    const { user, onSpeechModelChange, onModelChange } = setup({
      provider: "speech_voice_live",
      activeProvider: voiceProviderCatalog.providers[1],
      voice: voiceProviderCatalog.providers[1].capabilities.voices.default,
      speechModel: "gpt-4.1",
    });

    expect(screen.getByText(/Azure Speech chain · Azure Speech · eastus2/)).toBeInTheDocument();
    expect(screen.getByText(/GPT-4.1 response model paired/)).toBeInTheDocument();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Speech model" }),
      "gpt-5.1",
    );
    expect(onSpeechModelChange).toHaveBeenCalledWith("gpt-5.1");
    expect(onModelChange).not.toHaveBeenCalled();
  });

  it("edits Speech instructions and temperature without changing Azure OpenAI settings", async () => {
    const { user, onSettingsChange, onSpeechSettingsChange } = setup({
      provider: "speech_voice_live",
      activeProvider: voiceProviderCatalog.providers[1],
      voice: voiceProviderCatalog.providers[1].capabilities.voices.default,
      settings: { ...DEFAULT_VOICE_SETTINGS, instructions: "OpenAI instructions" },
      speechSettings: {
        ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
        instructions: "Speech instructions",
      },
    });

    const instructions = screen.getByRole("textbox", { name: "Instructions" });
    expect(instructions).toHaveValue("Speech instructions");
    fireEvent.change(instructions, { target: { value: "Updated speech" } });
    await user.type(screen.getByRole("spinbutton", { name: "Temperature" }), "0.7");

    expect(onSpeechSettingsChange).toHaveBeenCalled();
    expect(onSpeechSettingsChange).toHaveBeenLastCalledWith({
      ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
      instructions: "Speech instructions",
      temperature: 0.7,
    });
    expect(
      onSpeechSettingsChange.mock.calls.some(
        ([next]) => next.instructions === "Updated speech",
      ),
    ).toBe(true);
    expect(onSettingsChange).not.toHaveBeenCalled();
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
