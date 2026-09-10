import AppShell from "@/components/shell/AppShell";
import StrategyLeaderboard from "@/components/StrategyLeaderboard";

/**
 * Strategy leaderboard (Phase 59).
 *
 * A layout pass only, following `app/strategies/page.tsx`'s composition
 * shape: `AppShell` frames the page, `StrategyLeaderboard` owns every
 * fetch and every rendered state.
 */
export default function StrategyLeaderboardPage() {
  return (
    <AppShell
      title="Leaderboard"
      subtitle="Every strategy version the scorer has measured, ranked by score. A strategy with no completed backtest simply doesn't appear here."
    >
      <div className="flex flex-col gap-5">
        <StrategyLeaderboard />
      </div>
    </AppShell>
  );
}
