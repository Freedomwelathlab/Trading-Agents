import type { Metadata } from "next";
import AppShell from "@/components/shell/AppShell";
import LibraryWorkbench from "@/components/library/LibraryWorkbench";

export const metadata: Metadata = {
  title: "Strategy library · Trading OS",
};

/**
 * Published daily-bar strategies measured on real history (Phase 108,
 * D135). Research only: no order is placed from this page.
 */
export default function LibraryPage() {
  return (
    <AppShell
      title="Strategy library"
      subtitle="Published trend, momentum and reversal rules, measured on real adjusted daily history for US, Singapore, India, crypto and FX - net of costs, against buy-and-hold, with the years after each rule was published shown separately."
    >
      <LibraryWorkbench />
    </AppShell>
  );
}
