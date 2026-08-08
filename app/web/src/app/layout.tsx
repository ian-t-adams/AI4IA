import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "@/components/ThemeProvider";
import { ClientTelemetryBoot } from "@/components/ClientTelemetryBoot";

export const metadata: Metadata = {
  title: "AI4IA — Agentic Chat",
  description:
    "Governed multimodal agent chat for enterprise knowledge work, built on Azure AI Foundry.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <a href="#main" className="skip-link">
          Skip to main content
        </a>
        <ClientTelemetryBoot />
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
