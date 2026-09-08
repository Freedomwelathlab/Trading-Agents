import AppShell from "@/components/shell/AppShell";
import StrategyList from "@/components/StrategyList";

/**
 * Strategy Lab — list + create (Phase 54).
 *
 * A layout pass only, following `app/dashboard/page.tsx`'s composition
 * shape: `AppShell` frames the page, `StrategyList` owns every fetch and
 * every rendered state. Clicking a strategy's name goes to
 * `app/strategies/[strategyId]/page.tsx` for its version history and
 * builder.
 */
export default function StrategiesPage() {
  return (
    <AppShell
      title="Strategy Lab"
      subtitle="Define, version and validate trading strategies. Nothing here executes a trade or reads real market data — validation is purely structural."
    >
      <div className="flex flex-col gap-5">
        <StrategyList />
      </div>
    </AppShell>
  );
}
