import Link from "next/link";
import AppShell from "@/components/shell/AppShell";
import StrategyList from "@/components/StrategyList";
import { btnSecondary } from "@/components/ui/primitives";

/**
 * Strategy Lab — list + create (Phase 54).
 *
 * A layout pass only, following `app/dashboard/page.tsx`'s composition
 * shape: `AppShell` frames the page, `StrategyList` owns every fetch and
 * every rendered state. Clicking a strategy's name goes to
 * `app/strategies/[strategyId]/page.tsx` for its version history and
 * builder. The link to `/strategies/research` (Phase 67 / D085) is the one
 * piece of page chrome added on top of that: the research assistant is
 * advisory-only and produces nothing to list here, so it gets a plain
 * cross-link rather than a place in `StrategyList` itself.
 */
export default function StrategiesPage() {
  return (
    <AppShell
      title="Strategy Lab"
      subtitle="Define, version and validate trading strategies. Nothing here executes a trade or reads real market data — validation is purely structural."
    >
      <div className="flex flex-col gap-5">
        <div className="flex justify-end">
          <Link href="/strategies/research" className={btnSecondary}>
            Ask the research assistant
          </Link>
        </div>
        <StrategyList />
      </div>
    </AppShell>
  );
}
