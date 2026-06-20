import { AdminDashboard } from "@/components/AdminDashboard";

// Read at request time so the runtime auth env is never frozen into the static
// build (matches the chat page). Admin visibility is enforced server-side by
// require_admin; this route only renders the dashboard shell + forbidden view.
export const dynamic = "force-dynamic";

export default function AdminPage() {
  return <AdminDashboard />;
}
