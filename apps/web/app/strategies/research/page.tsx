import AppShell from "@/components/shell/AppShell";
import StrategyResearchAssistant from "@/components/StrategyResearchAssistant";

/**
 * AI strategy research assistant (Phase 67 / D085).
 *
 * A layout pass only, following `app/strategies/leaderboard/page.tsx`'s
 * composition shape: `AppShell` frames the page, `StrategyResearchAssistant`
 * owns every fetch and every rendered state.
 */
export default function StrategyResearchPage() {
  return (
    <AppShell
      title="Research assistant"
      subtitle="Describe a strategy idea in plain English and get back a structural draft — indicators, entry/exit rules, and position sizing. Nothing here is saved, backtested, or traded automatically."
    >
      <div className="flex flex-col gap-5">
        <StrategyResearchAssistant />
      </div>
    </AppShell>
  );
}
