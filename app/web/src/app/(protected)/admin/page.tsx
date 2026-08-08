import { AdminDashboard } from "@/components/AdminDashboard";

// Read at request time so protected-route runtime configuration is never frozen
// into the static build. Admin authorization remains server-authoritative.
export const dynamic = "force-dynamic";

export default function AdminPage() {
  return <AdminDashboard />;
}
