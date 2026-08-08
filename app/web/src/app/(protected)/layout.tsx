import { AuthProvider } from "@/components/AuthProvider";
import { CustomToolsProvider } from "@/components/CustomToolsProvider";
import { LibraryProvider } from "@/components/LibraryProvider";
import { VoiceLiveProvider } from "@/components/VoiceLiveProvider";
import { getAuthConfig } from "@/lib/authConfig";
import { getCustomToolsConfig } from "@/lib/customToolsConfig";
import { getLibraryConfig } from "@/lib/libraryConfig";
import { getVoiceLiveConfig } from "@/lib/voiceLiveConfig";

export default function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <AuthProvider config={getAuthConfig()}>
      <VoiceLiveProvider config={getVoiceLiveConfig()}>
        <LibraryProvider config={getLibraryConfig()}>
          <CustomToolsProvider config={getCustomToolsConfig()}>
            {children}
          </CustomToolsProvider>
        </LibraryProvider>
      </VoiceLiveProvider>
    </AuthProvider>
  );
}
