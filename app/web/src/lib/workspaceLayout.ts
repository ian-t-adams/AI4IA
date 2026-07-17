export type MobileDrawer = "sidebar" | "inspector" | null;

export function toggleMobileDrawer(
  current: MobileDrawer,
  target: Exclude<MobileDrawer, null>,
): MobileDrawer {
  return current === target ? null : target;
}

export function closeUnavailableMobileDrawer(
  current: MobileDrawer,
  sidebarAvailable: boolean,
  inspectorAvailable: boolean,
): MobileDrawer {
  if (current === "sidebar" && !sidebarAvailable) return null;
  if (current === "inspector" && !inspectorAvailable) return null;
  return current;
}
