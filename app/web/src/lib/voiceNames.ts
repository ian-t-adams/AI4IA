// Display names for voice ids. Azure Speech ids carry a locale, a name and a
// model family ("en-US-Ava:DragonHDLatestNeural", "en-US-AndrewNeural",
// "en-US-Harper:MAI-Voice-2.1-Flash"); people read "Ava (en-US, Dragon HD)".
// The id itself is still what every request and saved preference uses. Anything
// else (OpenAI voice names, custom voice ids) is shown as it is.
import { voiceProviderCatalog } from "./data/voice_provider_catalog";

const SPEECH_VOICE = /^([a-z]{2,3}-[A-Z][A-Za-z]{1,3})-([A-Z][A-Za-z]*?)(Multilingual)?(Neural)?(?::([A-Za-z0-9][A-Za-z0-9.-]*))?$/;

// Public-preview voices come from the catalog, never from the shape of an id.
const PREVIEW_VOICES: ReadonlySet<string> = new Set(
  voiceProviderCatalog.providers.flatMap((provider) =>
    "previewOptions" in provider.capabilities.voices
      ? [...provider.capabilities.voices.previewOptions]
      : [],
  ),
);

export function isPreviewVoice(voice: string): boolean {
  return PREVIEW_VOICES.has(voice);
}

function spaceWords(value: string): string {
  return value
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .trim();
}

function family(variant: string | undefined): string | null {
  if (!variant) return null;
  const words = spaceWords(variant.replace(/Neural$/, "").replace(/Latest/g, "")).replace(/-/g, " ");
  return words || null;
}

export function formatVoiceName(voice: string): string {
  const preview = isPreviewVoice(voice);
  const match = SPEECH_VOICE.exec(voice.trim());
  if (!match) return preview ? `${voice} (preview)` : voice;
  const [, locale, name, multilingual, , variant] = match;
  const details = [
    locale,
    multilingual ? "Multilingual" : null,
    family(variant),
    preview ? "preview" : null,
  ].filter((part): part is string => Boolean(part));
  return `${name} (${details.join(", ")})`;
}
