"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";

/**
 * Fetch a resource that belongs to a key, without resetting state inside
 * an effect (Phase 74, D092).
 *
 * Four panels on the Markets terminal all need the same thing: when the
 * symbol changes, stop showing the previous symbol's data immediately,
 * then load the new one. The obvious way to write that —
 *
 *     useEffect(() => { setData(null); void load(); }, [symbol]);
 *
 * — is what `react-hooks/set-state-in-effect` refuses, and the rule is
 * right: that reset is a second render pass triggered by the first, and
 * with several such panels on one page the cascades compound.
 *
 * So the stored value is TAGGED with the key it was fetched for, and
 * freshness is DERIVED during render instead of being restored by an
 * effect. Stale data is never shown because it is never selected, not
 * because something raced to clear it. One consequence worth knowing: a
 * slow response for an abandoned key can still arrive, and it is simply
 * not selected — there is no torn state where the header says one symbol
 * and the table shows another.
 *
 * `classify` decides what a non-OK response MEANS, which differs per
 * endpoint and is the part a shared hook must not assume: a 404 from the
 * depth endpoint is an ordinary absence (no book right now), while a 404
 * from a strategy endpoint would be a genuine error.
 *
 * **`classify` must be stable** — wrap it in `useMemo` at the call site.
 * It is a dependency of the loader rather than being stashed in a ref:
 * mutating a ref during render is its own violation, and every caller
 * here builds its classifier from constants anyway, so a memo costs
 * nothing and keeps the dependency honest.
 */

export type FetchOutcome<T> =
  | { kind: "ok"; value: T }
  | { kind: "unavailable"; detail: string }
  | { kind: "error"; detail: string };

export type KeyedFetchState<T> = {
  /** Non-null only when the loaded data belongs to the CURRENT key. */
  data: T | null;
  unavailable: string | null;
  error: string | null;
  /** True until an outcome for the current key has arrived. */
  loading: boolean;
  reload: () => void;
};

export function useKeyedFetch<T>({
  key,
  url,
  classify,
  refreshMs,
  enabled = true,
}: {
  /** Identifies the inputs. A change here makes existing data stale. */
  key: string;
  url: string;
  classify: (status: number, body: unknown) => FetchOutcome<T>;
  /** Poll interval. Omitted means fetch once per key. */
  refreshMs?: number;
  enabled?: boolean;
}): KeyedFetchState<T> {
  const [state, setState] = useState<{ key: string; outcome: FetchOutcome<T> } | null>(null);
  const run = useCallback(async () => {
    let r: Response;
    try {
      r = await fetch(url, { cache: "no-store" });
    } catch {
      setState({
        key,
        outcome: { kind: "error", detail: "Could not reach the trading API." },
      });
      return;
    }
    if (handleExpiredSession(r.status)) return;
    const body = await r.json().catch(() => null);
    setState({ key, outcome: classify(r.status, body) });
  }, [key, url, classify]);

  useEffect(() => {
    if (!enabled) return;
    // Deferred by one microtask so the loader's first `setState` lands
    // AFTER this effect returns rather than during it — the same
    // convention `Watchlist.tsx` established, and the reason is the same:
    // calling it synchronously re-renders from inside the effect React is
    // still committing, which `react-hooks/set-state-in-effect` flags as a
    // cascading render.
    void Promise.resolve().then(run);
    if (!refreshMs) return;
    // Interval ticks are already outside the commit, so they need no defer.
    const id = setInterval(() => void run(), refreshMs);
    return () => clearInterval(id);
  }, [run, refreshMs, enabled]);

  // Derived, not restored: data for another key is simply not selected.
  const fresh = state && state.key === key ? state.outcome : null;

  return {
    data: fresh?.kind === "ok" ? fresh.value : null,
    unavailable: fresh?.kind === "unavailable" ? fresh.detail : null,
    error: fresh?.kind === "error" ? fresh.detail : null,
    loading: fresh === null,
    reload: () => void run(),
  };
}

/** The common shape: 404/503 are absences, anything else non-OK is an error. */
export function classifyWithAbsences<T>(
  absentStatuses: number[],
  fallbackAbsent: string,
): (status: number, body: unknown) => FetchOutcome<T> {
  return (status, body) => {
    const detail =
      body && typeof body === "object" && typeof (body as { detail?: unknown }).detail === "string"
        ? ((body as { detail: string }).detail)
        : null;
    if (status >= 200 && status < 300) {
      return { kind: "ok", value: body as T };
    }
    if (absentStatuses.includes(status)) {
      return { kind: "unavailable", detail: detail ?? fallbackAbsent };
    }
    return { kind: "error", detail: detail ?? `Request failed (${status}).` };
  };
}
