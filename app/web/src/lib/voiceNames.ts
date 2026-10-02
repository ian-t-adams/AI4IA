// Display names for voice ids. Azure Speech ids carry a locale, a name and a
// model family ("en-US-Ava:DragonHDLatestNeural", "en-US-AndrewNeural"); people
// read "Ava (en-US, Dragon HD)". The id itself is still what every request and
// saved preference uses. Anything else (OpenAI voice names, custom voice ids)
// is shown as it is.
const SPEECH_VOICE = /^([a-z]{2,3}-[A-Z][A-Za-z]{1,3})-([A-Z][A-Za-z]*?)(Multilingual)?(Neural)?(?::([A-Za-z0-9]+))?$/;

function spaceWords(value: string): string {
  return value
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .trim();
}

function family(variant: string | undefined): string | null {
  if (!variant) return null;
  const words = spaceWords(variant.replace(/Neural$/, "").replace(/Latest/g, ""));
  return words || null;
}

export function formatVoiceName(voice: string): string {
  const match = SPEECH_VOICE.exec(voice.trim());
  if (!match) return voice;
  const [, locale, name, multilingual, , variant] = match;
  const details = [locale, multilingual ? "Multilingual" : null, family(variant)].filter(
    (part): part is string => Boolean(part),
  );
  return `${name} (${details.join(", ")})`;
}
