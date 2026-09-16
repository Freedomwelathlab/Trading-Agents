"use client";

import { useCallback, useEffect, useState } from "react";
import {
  EXPIRING_SOON_SECONDS,
  SESSION_ENDPOINT,
  SessionInfo,
  formatRemaining,
  handleExpiredSession,
} from "@/lib/session";

const POLL_MS = 60_000;

/**
 * Shows who is signed in and how much of the session is actually left
 * (D032).
 *
 * The JWT lives in an httpOnly cookie (D020), so this cannot be read
 * client-side and is never guessed: the number shown is
 * `expires_in_seconds` as computed by the backend against its own clock
 * on the most recent poll, re-fetched every minute. Between polls the
 * component does NOT tick a local countdown down — a locally-decremented
 * number would drift from the server's view the moment the tab is
 * backgrounded or the machine suspends, which is exactly the kind of
 * fabricated figure this project forbids.
 *
 * When the poll comes back 401, the shared handler sends the user to the
 * login page with a real reason instead of leaving a dead dashboard on
 * screen.
 */
export default function SessionStatus() {
  const [info, setInfo] = useState<SessionInfo | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);

  const poll = useCallback(async () => {
    try {
      const res = await fetch(SESSION_ENDPOINT, { cache: "no-store" });
      const data = (await res.json().catch(() => null)) as
        | (SessionInfo & { detail?: string })
        | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setInfo(null);
        setErrorDetail(data?.detail ?? `Session check failed (HTTP ${res.status})`);
        return;
      }
      setErrorDetail(null);
      setInfo(data);
    } catch {
      setInfo(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  useEffect(() => {
    // Deferred by one microtask so the loader's first `setState` lands
    // AFTER this effect returns rather than during it - calling it
    // synchronously re-renders from inside the effect React is still
    // committing, which `react-hooks` flags as a cascading render.
    void Promise.resolve().then(poll);
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  if (errorDetail) {
    return (
      <p
        role="status"
        className="rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs leading-relaxed text-amber-900 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300"
        data-testid="session-status"
      >
        Session status unavailable — {errorDetail}
      </p>
    );
  }

  if (!info) return null;

  const soon = info.expires_in_seconds <= EXPIRING_SOON_SECONDS;

  return (
    <p
      role="status"
      data-testid="session-status"
      className={
        soon
          ? "rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs font-medium leading-relaxed text-amber-900 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300"
          : "px-1 text-xs leading-relaxed text-ink-faint"
      }
    >
      {soon ? "Session expiring soon — " : ""}
      <span className="font-mono text-ink-muted">{info.email}</span> ·{" "}
      <span className="tnum font-mono">
        {formatRemaining(info.expires_in_seconds)}
      </span>{" "}
      left
      {soon ? " (sign out and back in to continue)" : ""}
    </p>
  );
}
