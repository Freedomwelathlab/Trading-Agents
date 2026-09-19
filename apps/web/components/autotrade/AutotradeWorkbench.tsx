"use client";

import { useState } from "react";
import CreateBotForm from "./CreateBotForm";
import BotList from "./BotList";

/** Glue: a created bot bumps `refreshKey` so the list re-reads. */
export default function AutotradeWorkbench() {
  const [refreshKey, setRefreshKey] = useState(0);
  return (
    <div className="flex flex-col gap-5">
      <CreateBotForm onCreated={() => setRefreshKey((k) => k + 1)} />
      <BotList refreshKey={refreshKey} />
    </div>
  );
}
