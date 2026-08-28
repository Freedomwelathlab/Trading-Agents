import HealthStatus from "@/components/HealthStatus";
import QuoteLookup from "@/components/QuoteLookup";
import TradeForm from "@/components/TradeForm";
import LogoutButton from "@/components/LogoutButton";

export default function DashboardPage() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <LogoutButton />
      </div>
      <HealthStatus />
      <QuoteLookup />
      <TradeForm />
    </main>
  );
}
