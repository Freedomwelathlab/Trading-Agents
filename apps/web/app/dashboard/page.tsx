import AppShell from "@/components/shell/AppShell";
import { SectionHeading } from "@/components/ui/primitives";
import HealthStatus from "@/components/HealthStatus";
import QuoteLookup from "@/components/QuoteLookup";
import BrokerDiscovery from "@/components/BrokerDiscovery";
import TradeForm from "@/components/TradeForm";
import AgentTradeForm from "@/components/AgentTradeForm";
import PortfolioView from "@/components/PortfolioView";
import PortfolioHistoryChart from "@/components/PortfolioHistoryChart";
import BacktestPanel from "@/components/BacktestPanel";
import Watchlist from "@/components/Watchlist";

/**
 * The trading desk (Phase 45 / D060).
 *
 * A layout pass only: every panel below is the same component, fetching
 * the same endpoint and rendering the same real backend states —
 * including every `NOT_CONFIGURED:` / `DATA_UNAVAILABLE:` sentinel, every
 * 403, and the Portfolio Manager's separate verdict (D029) — as it did
 * before. What changed is where they sit relative to each other.
 *
 * The order is not arbitrary:
 *
 * - `BrokerDiscovery` stays ahead of every broker-scoped panel, because
 *   it is where the `broker_id` those panels need comes from (D034).
 * - Portfolio state and its equity curve lead, and get the wide column:
 *   they are what an operator reads first and they are the densest.
 * - The two order-entry panels sit together under their own heading and
 *   share a row, so neither reads as the default action.
 * - `BacktestPanel` is last and separate because, unlike everything above
 *   it, it is not broker-scoped at all — a run builds its own throwaway
 *   paper broker and writes nothing (D025).
 *
 * Phase 50 adds `Watchlist` under its own "Research" heading, next to
 * `QuoteLookup`. Both are user-scoped, not broker-scoped — neither takes
 * a `broker_id` and neither consults a `BrokerGrant` — so by D034's
 * ordering convention they belong outside the broker-scoped block, not
 * inside it. They are also the two panels that answer the same question
 * at different widths: one symbol, or the whole list you are following.
 */
export default function DashboardPage() {
  return (
    <AppShell
      title="Trading desk"
      subtitle="Live account state, order entry and simulation. Every figure on this page is one the backend returned; nothing is placeholder data."
      statusStrip={<HealthStatus />}
    >
      <div className="flex flex-col gap-5">
        <SectionHeading note="Selects the broker every panel below is scoped to">
          Broker context
        </SectionHeading>
        <BrokerDiscovery />

        <SectionHeading>Position &amp; performance</SectionHeading>
        <div className="grid gap-5 xl:grid-cols-12">
          <PortfolioView className="xl:col-span-7" />
          <PortfolioHistoryChart className="xl:col-span-5" />
        </div>

        <SectionHeading note="Not broker-scoped — no grant required">
          Research
        </SectionHeading>
        <div className="grid gap-5 xl:grid-cols-12">
          <Watchlist className="xl:col-span-7" />
          <QuoteLookup className="xl:col-span-5" />
        </div>

        <SectionHeading note="Both paths share one deterministic Risk Engine">
          Order entry
        </SectionHeading>
        <div className="grid gap-5 xl:grid-cols-2">
          <TradeForm />
          <AgentTradeForm />
        </div>

        <SectionHeading note="Throwaway paper broker — writes nothing">
          Simulation
        </SectionHeading>
        <BacktestPanel />
      </div>
    </AppShell>
  );
}
