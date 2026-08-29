import Link from "next/link";
import HealthStatus from "@/components/HealthStatus";
import QuoteLookup from "@/components/QuoteLookup";
import BrokerDiscovery from "@/components/BrokerDiscovery";
import TradeForm from "@/components/TradeForm";
import AgentTradeForm from "@/components/AgentTradeForm";
import PortfolioView from "@/components/PortfolioView";
import PortfolioHistoryChart from "@/components/PortfolioHistoryChart";
import BacktestPanel from "@/components/BacktestPanel";
import SessionStatus from "@/components/SessionStatus";
import LogoutButton from "@/components/LogoutButton";

export default function DashboardPage() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Dashboard</h1>
          <SessionStatus />
        </div>
        <div className="flex items-center gap-3">
          {/* Always shown: whether this account actually holds admin:manage
              is determined server-side by the backend on submit, never
              guessed here to decide what to render. */}
          <Link
            href="/admin"
            className="rounded border border-neutral-300 px-3 py-1.5 text-sm dark:border-neutral-700"
          >
            Admin
          </Link>
          <LogoutButton />
        </div>
      </div>
      <HealthStatus />
      <QuoteLookup />
      {/* Above the broker-scoped forms on purpose: it is where the
          broker_id those forms need comes from (D034). */}
      <BrokerDiscovery />
      <TradeForm />
      <AgentTradeForm />
      <PortfolioView />
      <PortfolioHistoryChart />
      {/* Not broker-scoped, unlike everything above it: a backtest builds
          its own throwaway paper broker and writes nothing (D025). */}
      <BacktestPanel />
    </main>
  );
}
