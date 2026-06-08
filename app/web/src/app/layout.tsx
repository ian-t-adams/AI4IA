import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "@/components/ThemeProvider";
import { AuthProvider } from "@/components/AuthProvider";
import { getAuthConfig } from "@/lib/authConfig";

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
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <a href="#main" className="visually-hidden">
          Skip to main content
        </a>
        <ThemeProvider>
          <AuthProvider config={authConfig}>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
