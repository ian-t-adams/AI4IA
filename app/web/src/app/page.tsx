import { ChatApp } from "@/components/ChatApp";

// Read at request time in the container so the runtime auth env (read in the
// root layout) is never frozen into the static build.
export const dynamic = "force-dynamic";

export default function Page() {
  return <ChatApp />;
}
