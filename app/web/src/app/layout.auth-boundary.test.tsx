import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import RootLayout from "./layout";
import ProtectedLayout from "./(protected)/layout";

vi.mock("@/components/ClientTelemetryBoot", () => ({
  ClientTelemetryBoot: () => null,
}));
vi.mock("@/components/ThemeProvider", () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="theme-boundary">{children}</div>
  ),
}));
vi.mock("@/components/AuthProvider", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="auth-boundary">{children}</div>
  ),
}));
vi.mock("@/components/VoiceLiveProvider", () => ({
  VoiceLiveProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/LibraryProvider", () => ({
  LibraryProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/CustomToolsProvider", () => ({
  CustomToolsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/authConfig", () => ({ getAuthConfig: () => ({ provider: "dev" }) }));
vi.mock("@/lib/voiceLiveConfig", () => ({ getVoiceLiveConfig: () => ({}) }));
vi.mock("@/lib/libraryConfig", () => ({ getLibraryConfig: () => ({}) }));
vi.mock("@/lib/customToolsConfig", () => ({ getCustomToolsConfig: () => ({}) }));

describe("app authentication boundary", () => {
  it("keeps unknown-route content outside authentication", () => {
    const html = renderToStaticMarkup(
      <RootLayout>
        <main>Custom 404</main>
      </RootLayout>,
    );

    expect(html).toContain("Custom 404");
    expect(html).toContain('data-testid="theme-boundary"');
    expect(html).not.toContain('data-testid="auth-boundary"');
  });

  it("wraps valid application routes in authentication", () => {
    const html = renderToStaticMarkup(
      <ProtectedLayout>
        <main>Known application route</main>
      </ProtectedLayout>,
    );

    expect(html).toContain("Known application route");
    expect(html).toContain('data-testid="auth-boundary"');
  });
});
