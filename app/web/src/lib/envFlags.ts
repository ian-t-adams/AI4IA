const TRUTHY = new Set(["1", "true", "yes", "on"]);

export function parseEnabledFlag(raw: string | null | undefined): boolean {
  return TRUTHY.has((raw ?? "").trim().toLowerCase());
}
