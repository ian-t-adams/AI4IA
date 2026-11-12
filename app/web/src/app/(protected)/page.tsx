import { AuthenticatedChatApp } from "@/components/AuthenticatedChatApp";
import { getChatPilotUiConfig } from "@/lib/chatPilotUiConfig";

// Read at request time so protected-route runtime configuration is never frozen
// into the static build.
export const dynamic = "force-dynamic";

export default function Page() {
  return (
    <AuthenticatedChatApp
      chatPilotCompatibilityEnabled={getChatPilotUiConfig().enabled}
    />
  );
}
