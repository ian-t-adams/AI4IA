import { ChatApp } from "@/components/ChatApp";

// Read at request time so protected-route runtime configuration is never frozen
// into the static build.
export const dynamic = "force-dynamic";

export default function Page() {
  return <ChatApp />;
}
