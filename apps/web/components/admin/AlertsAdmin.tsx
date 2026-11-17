"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { Alert, Panel, btnSecondary } from "@/components/ui/primitives";

/**
 * Alert channel check (Phase 111, D138). One button that asks the backend
 * to send a fixed test message to the configured Telegram chat.
 *
 * "Delivered" is shown only when the backend says the channel accepted the
 * message. NOT_CONFIGURED and a refusal are shown in the backend's own
 * words - this panel never decides for itself that an alert went out.
 */

type Outcome = { ok: true; channel: string } | { ok: false; detail: string };

export function AlertChannelPanel() {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  async function send() {
    setBusy(true);
    setOutcome(null);
    try {
      const res = await fetch("/api/admin/alerts/test", { method: "POST" });
      if (handleExpiredSession(res.status)) return;
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (res.ok && data?.delivered === true) {
        setOutcome({ ok: true, channel: String(data.channel ?? "channel") });
        return;
      }
      const detail = data?.detail;
      setOutcome({
        ok: false,
        detail: typeof detail === "string" ? detail : `Request failed (HTTP ${res.status})`,
      });
    } catch {
      setOutcome({ ok: false, detail: "DATA_UNAVAILABLE: could not reach the trading API" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Alert channel"
      description="Every bot sends one Telegram message when it trades, is refused an order, is paused by its kill switch, or fails. Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on the server; without both there is no channel and nothing is sent."
    >
      <button type="button" className={btnSecondary} onClick={() => void send()} disabled={busy}>
        {busy ? "Sending…" : "Send test alert"}
      </button>
      {outcome?.ok ? (
        <Alert tone="info" role="status" className="mt-3" testId="alert-test-ok">
          Delivered: {outcome.channel} accepted the test message. Check the chat.
        </Alert>
      ) : null}
      {outcome && !outcome.ok ? (
        <Alert className="mt-3" testId="alert-test-error">{outcome.detail}</Alert>
      ) : null}
    </Panel>
  );
}
