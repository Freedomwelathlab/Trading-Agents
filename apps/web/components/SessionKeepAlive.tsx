"use client";

import { useEffect, useRef } from "react";

/**
 * Idle-aware session (Phase 84, D100).
 *
 * The token expires 15 minutes after it was issued. This component renews
 * it — silently, through `POST /api/auth/refresh` — as long as the person
 * has touched the keyboard, mouse or screen inside the last `IDLE_MS`.
 * Stop touching it and the renewals stop, so the session ends on the
 * backend's own expiry, exactly as before. No client-side timer decides
 * anything about validity: the backend still 401s when it 401s.
 *
 * Renewal cadence is once a minute while active. A renewal that fails
 * with 401 is left to `SessionStatus`, which already sends the person to
 * the login page with the real reason.
 */

const IDLE_MS = 15 * 60_000;
const CHECK_MS = 60_000;
const ACTIVITY_EVENTS = ["mousemove", "mousedown", "keydown", "scroll", "touchstart", "wheel"];

export default function SessionKeepAlive() {
  const lastActivity = useRef<number>(0);

  useEffect(() => {
    lastActivity.current = Date.now();  // mounting counts as activity
    const mark = () => {
      lastActivity.current = Date.now();
    };
    for (const ev of ACTIVITY_EVENTS) window.addEventListener(ev, mark, { passive: true });

    const timer = setInterval(() => {
      if (document.visibilityState === "hidden") return;
      if (Date.now() - lastActivity.current > IDLE_MS) return;
      void fetch("/api/auth/refresh", { method: "POST", cache: "no-store" }).catch(() => {});
    }, CHECK_MS);

    return () => {
      for (const ev of ACTIVITY_EVENTS) window.removeEventListener(ev, mark);
      clearInterval(timer);
    };
  }, []);

  return null;
}
