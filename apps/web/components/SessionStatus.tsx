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
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  if (errorDetail) {
    return (
      <p
        role="status"
        className="text-xs text-amber-700 dark:text-amber-400"
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
          ? "rounded bg-amber-50 px-2 py-1 text-xs font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-300"
          : "text-xs text-neutral-500"
      }
    >
      {soon ? "Session expiring soon — " : ""}
      {info.email} · {formatRemaining(info.expires_in_seconds)} left
      {soon ? " (sign out and back in to continue)" : ""}
    </p>
  );
}
