import type { Metadata } from "next";
import AppShell from "@/components/shell/AppShell";
import AutotradeWorkbench from "@/components/autotrade/AutotradeWorkbench";

export const metadata: Metadata = {
  title: "Autotrade · Trading OS",
};

/**
 * The Autotrade Bot page (Phase 81, D098): create a robot from the
 * operator's inputs, approve it, watch its cycles, trades and the learning
 * loop. One client island, same composition shape as every other page.
 */
export default function AutotradePage() {
  return (
    <AppShell
      title="Autotrade"
      subtitle="Intraday robots on paper brokers. Every entry and exit goes through the same risk engine as a manual trade; a bot trades only after you approve it, and is flat by the end of its session."
    >
      <AutotradeWorkbench />
    </AppShell>
  );
}
