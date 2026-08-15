// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { VoiceSettingsPanel, type VoiceSettingsPanelProps } from "./VoiceSettingsPanel";
import {
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_VOICE_SETTINGS,
} from "@/lib/voiceLive";
import { voiceProviderCatalog } from "@/lib/data/voice_provider_catalog";

afterEach(() => {
  cleanup();
});

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
  const onProviderChange = vi.fn();
  const onModelChange = vi.fn();
  const onVoiceChange = vi.fn();
  const onSpeechModelChange = vi.fn();
  const onSettingsChange = vi.fn();
  const onSpeechSettingsChange = vi.fn();
  const onReset = vi.fn();
  const props: VoiceSettingsPanelProps = {
    providers: PROVIDERS,
    provider: "azure_openai",
    onProviderChange,
    activeProvider: voiceProviderCatalog.providers[0],
    models: MODELS,
    defaultModelLabel: "Default (GPT Realtime)",
    explicitModel: null,
    onModelChange,
    speechModel: "gpt-realtime",
    onSpeechModelChange,
    voice: "alloy",
    onVoiceChange,
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
    onProviderChange,
    onModelChange,
    onVoiceChange,
    onSpeechModelChange,
    onSettingsChange,
    onSpeechSettingsChange,
    onReset,
  };
}

describe("VoiceSettingsPanel", () => {
  it("renders controls directly without a nested disclosure or dialog", () => {
    setup();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.querySelector("details")).toBeNull();
    expect(screen.queryByText("Voice settings")).toBeNull();
    expect(screen.getByRole("combobox", { name: "Provider" })).toBeInTheDocument();
  });

  it("offers the server-advertised providers and defaults to Azure OpenAI", () => {
    setup();
    const select = screen.getByRole("combobox", { name: "Provider" });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Azure OpenAI",
      "Azure Speech",
    ]);
    expect(select).toHaveAccessibleDescription(PROVIDERS[0].description);
    for (const option of within(select).getAllByRole("option")) {
      expect(option).not.toHaveAttribute("title");
    }
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

  it("exposes advanced audio controls without an instructions field", () => {
    setup();
    expect(screen.queryByRole("textbox", { name: "Instructions" })).toBeNull();
    expect(screen.getByRole("spinbutton", { name: "Temperature" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Playback stability" })).toHaveValue(
      "balanced",
    );
    expect(screen.getByRole("combobox", { name: "Turn detection" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "VAD threshold" })).toBeInTheDocument();
    expect(
      screen.getByRole("spinbutton", { name: "Reply after silence (ms)" }),
    ).toBeInTheDocument();
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
    expect(screen.queryByRole("combobox", { name: "Locale" })).toBeNull();
    const model = screen.getByRole("combobox", { name: "Speech model" });
    expect(within(model).getAllByRole("option")).toHaveLength(6);
    expect(screen.queryByRole("combobox", { name: "Transcription" })).toBeNull();
    expect(screen.getByText(/Native audio · GPT-4o Transcribe · eastus2/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Turn detection" })).toBeInTheDocument();
    expect(screen.getByText("Managed by Azure Speech")).toBeInTheDocument();
    expect(screen.getByText(/deep noise suppression and echo cancellation/)).toBeInTheDocument();
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

  it("edits Speech temperature without changing Azure OpenAI settings", async () => {
    const { user, onSettingsChange, onSpeechSettingsChange } = setup({
      provider: "speech_voice_live",
      activeProvider: voiceProviderCatalog.providers[1],
      voice: voiceProviderCatalog.providers[1].capabilities.voices.default,
      settings: DEFAULT_VOICE_SETTINGS,
      speechSettings: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
    });

    await user.type(screen.getByRole("spinbutton", { name: "Temperature" }), "0.7");

    expect(onSpeechSettingsChange).toHaveBeenCalled();
    expect(onSpeechSettingsChange).toHaveBeenLastCalledWith({
      ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
      temperature: 0.7,
    });
    expect(onSettingsChange).not.toHaveBeenCalled();
  });

  it("changes the shared browser playback profile without changing provider settings", async () => {
    const { user, onSettingsChange, onSpeechSettingsChange } = setup({
      provider: "speech_voice_live",
      activeProvider: voiceProviderCatalog.providers[1],
      voice: voiceProviderCatalog.providers[1].capabilities.voices.default,
    });

    const playback = screen.getByRole("combobox", { name: "Playback stability" });
    expect(playback).toHaveAccessibleDescription(
      "Higher stability adds a little delay to smooth network jitter.",
    );
    await user.selectOptions(playback, "smooth");

    expect(onSettingsChange).toHaveBeenCalledWith({
      ...DEFAULT_VOICE_SETTINGS,
      playbackProfile: "smooth",
    });
    expect(onSpeechSettingsChange).not.toHaveBeenCalled();
  });

  it("calls onReset when Reset defaults is clicked", async () => {
    const { user, onReset } = setup();
    await user.click(screen.getByRole("button", { name: "Reset defaults" }));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("disables every control while locked but keeps them visible", () => {
    setup({ locked: true });
    expect(screen.getByRole("combobox", { name: "Realtime model" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Voice" })).toBeDisabled();
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
