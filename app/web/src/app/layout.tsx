import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "@/components/ThemeProvider";
import { AuthProvider } from "@/components/AuthProvider";
import { VoiceLiveProvider } from "@/components/VoiceLiveProvider";
import { LibraryProvider } from "@/components/LibraryProvider";
import { CustomToolsProvider } from "@/components/CustomToolsProvider";
import { getAuthConfig } from "@/lib/authConfig";
import { getVoiceLiveConfig } from "@/lib/voiceLiveConfig";
import { getLibraryConfig } from "@/lib/libraryConfig";
import { getCustomToolsConfig } from "@/lib/customToolsConfig";

export const metadata: Metadata = {
  title: "AI4IA — Agentic Chat",
  description:
    "Multi-model agentic chat for personal use and customer demos, powered by Azure AI Foundry.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const authConfig = getAuthConfig();
  const voiceLiveConfig = getVoiceLiveConfig();
  const libraryConfig = getLibraryConfig();
  const customToolsConfig = getCustomToolsConfig();
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <a href="#main" className="visually-hidden">
          Skip to main content
        </a>
        <ThemeProvider>
          <AuthProvider config={authConfig}>
            <VoiceLiveProvider config={voiceLiveConfig}>
              <LibraryProvider config={libraryConfig}>
                <CustomToolsProvider config={customToolsConfig}>
                  {children}
                </CustomToolsProvider>
              </LibraryProvider>
            </VoiceLiveProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
