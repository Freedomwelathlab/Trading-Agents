"use client";

import { useEffect, useState } from "react";
import OptionsBotPanel from "@/components/options/OptionsBotPanel";
import AssetBotsWorkbench from "./AssetBotsWorkbench";

/**
 * Separate bots per asset class (Phase 107, D133): crypto, forex and
 * options, each with its own create form, its own bots and a scan
 * dashboard. Equity intraday bots stay on the Autotrade page.
 */

const TABS = [
  { id: "crypto", label: "Crypto", note: "24 hours · Coinbase" },
  { id: "forex", label: "Forex", note: "Sun–Fri · Twelve Data" },
  { id: "options", label: "Options", note: "US session · Cboe chains" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function BotsWorkbench() {
  const [tab, setTab] = useState<TabId>("crypto");

  // A shared link may open a tab directly (#forex, #options). Read after
  // mount so the server render and the first client render agree.
  useEffect(() => {
    const hash = window.location.hash.replace("#", "");
    const found = TABS.find((t) => t.id === hash);
    if (found) void Promise.resolve().then(() => setTab(found.id));
  }, []);

  function choose(id: TabId) {
    setTab(id);
    try {
      window.history.replaceState(null, "", `#${id}`);
    } catch {
      /* hash is a convenience only */
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <div role="tablist" aria-label="Bot type" className="flex flex-wrap gap-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            id={`bots-tab-${t.id}`}
            role="tab"
            type="button"
            aria-selected={tab === t.id}
            onClick={() => choose(t.id)}
            className={`flex flex-col items-start rounded-lg border px-4 py-2 text-left transition-colors ${
              tab === t.id ? "border-accent bg-raised text-ink" : "border-line bg-surface text-ink-muted hover:text-ink"
            }`}
          >
            <span className="text-sm font-semibold">{t.label} bot</span>
            <span className="font-mono text-[11px] text-ink-faint">{t.note}</span>
          </button>
        ))}
      </div>
      <div role="tabpanel" aria-labelledby={`bots-tab-${tab}`}>
        {tab === "crypto" ? <AssetBotsWorkbench key="crypto" assetClass="crypto" /> : null}
        {tab === "forex" ? <AssetBotsWorkbench key="forex" assetClass="forex" /> : null}
        {tab === "options" ? (
          <OptionsBotPanel underlying="TQQQ.US" defaultStructure="signal" showDashboard />
        ) : null}
      </div>
    </div>
  );
}
