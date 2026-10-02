import type { Metadata } from "next";
import AppShell from "@/components/shell/AppShell";
import BotsWorkbench from "@/components/bots/BotsWorkbench";

export const metadata: Metadata = {
  title: "Bots · Trading OS",
};

/**
 * Crypto, forex and options bots, each with its own scan dashboard
 * (Phase 107, D133/D134). Paper brokers only; every order goes through the
 * same Risk Engine as a manual trade.
 */
export default function BotsPage() {
  return (
    <AppShell
      title="Bots"
      subtitle="Separate paper-trading bots for crypto, forex and options. Each dashboard shows every setup's scan score on the latest bar and the recommendation it adds up to, beside what the research actually measured."
    >
      <BotsWorkbench />
    </AppShell>
  );
}
