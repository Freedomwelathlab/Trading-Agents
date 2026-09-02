"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";

/**
 * Admin UI for the two Phase 43 / D058 broker endpoints — `POST
 * /admin/brokers` and `PATCH /admin/brokers/{broker_id}/mode`.
 *
 * D058 built and tested both backend-side but deliberately shipped no
 * frontend, on the grounds that "a live-trading UI deserves its own
 * reviewed phase". This is that UI (Phase 47). It is frontend only: both
 * forms post through a route handler to the untouched backend, which
 * remains the sole enforcer of every rule below.
 *
 * Three things here are load-bearing rather than cosmetic:
 *
 * 1. `confirm_live` is sent ONLY when the operator has ticked the
 *    dedicated confirmation checkbox AND the target kind is `live`. It is
 *    never pre-checked, never defaulted to true, and never inferred from
 *    the fact that the user pressed submit. Ticking it is a separate,
 *    separately-labelled act from submitting the form, which is the whole
 *    point of the backend requiring it.
 *
 * 2. Submitting without that tick is NOT blocked client-side. The request
 *    goes to the backend without `confirm_live` and the backend's own 400
 *    (`LIVE_KIND_CONFIRMATION_REQUIRED`) is rendered verbatim. D058's own
 *    reasoning applies: a confirmation that exists only in the frontend is
 *    not one the server can enforce, so the server must stay the thing
 *    that refuses — and the operator should see it refuse.
 *
 * 3. Changing the target kind resets the checkbox. Otherwise a tick made
 *    while "live" was selected could survive a switch to "paper" and back,
 *    which would let a stale confirmation authorise a designation the
 *    operator never re-considered.
 *
 * Nothing in this file enables live trading. Designating a broker
 * `kind: "live"` only makes it *eligible* for the real-money path; an
 * actual order additionally requires TRADING_MODE=live,
 * LIVE_TRADING_ENABLED=true, the LONGPORT_LIVE_* credentials, the
 * `trade:submit:live` permission, and `confirm: true` on that individual
 * request. None of those are reachable from this page.
 */

type BrokerResponse = {
  id?: string;
  name?: string;
  kind?: string;
  provider?: string;
  is_active?: boolean;
  detail?: string;
};

type BrokerKind = "paper" | "live";

/**
 * Identical in shape and styling to `UsersAdmin`'s `ResultOrError`: a real
 * backend error is rendered with its status and the backend's own
 * `detail` string, never a friendlier local paraphrase. The 409 for a
 * broker with recorded orders and the 400 for a missing `confirm_live`
 * both arrive here as-is.
 */
function ResultOrError({
  status,
  errorDetail,
  result,
}: {
  status: number | null;
  errorDetail: string | null;
  result: BrokerResponse | null;
}) {
  if (errorDetail) {
    return (
      <p
        role="alert"
        className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm leading-relaxed text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300"
      >
        {status ? `HTTP ${status}: ` : ""}
        {errorDetail}
      </p>
    );
  }
  if (result) {
    return (
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-ink-faint">id</dt>
        <dd className="font-mono">{result.id}</dd>
        <dt className="text-ink-faint">name</dt>
        <dd>{result.name}</dd>
        <dt className="text-ink-faint">kind</dt>
        <dd>{result.kind}</dd>
        <dt className="text-ink-faint">provider</dt>
        <dd>{result.provider}</dd>
        <dt className="text-ink-faint">is_active</dt>
        <dd>{String(result.is_active)}</dd>
      </dl>
    );
  }
  return null;
}

const inputClass =
  "w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50";

const submitClass =
  "inline-flex cursor-pointer items-center justify-center gap-2 rounded-md text-sm font-semibold transition-all duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 bg-accent px-4 py-2 text-accent-ink shadow-sm hover:brightness-110 active:brightness-95 self-start";

/**
 * The confirmation control shared by both forms. Rendered at all times so
 * its existence (and the consequence it describes) is visible before the
 * operator picks `live`, but only enabled — and only ever sent — when
 * `live` is the selected target.
 */
function LiveConfirmation({
  kind,
  checked,
  onChange,
  idPrefix,
}: {
  kind: BrokerKind;
  checked: boolean;
  onChange: (v: boolean) => void;
  idPrefix: string;
}) {
  const isLive = kind === "live";
  return (
    <div
      className={
        "rounded-md border px-3 py-2.5 " +
        (isLive
          ? "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/60"
          : "border-line bg-well")
      }
    >
      <label className="flex items-start gap-2.5 text-sm">
        <input
          id={`${idPrefix}-confirm-live`}
          type="checkbox"
          className="mt-0.5 shrink-0"
          checked={checked}
          disabled={!isLive}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span
          className={
            isLive
              ? "leading-relaxed text-red-800 dark:text-red-300"
              : "leading-relaxed text-ink-muted"
          }
        >
          I understand this designates a REAL, LIVE trading broker, and that once
          this broker has recorded any order its kind can no longer be changed.
        </span>
      </label>
      <p className="mt-1.5 pl-[26px] text-xs leading-relaxed text-ink-faint">
        {isLive
          ? "Sent as confirm_live: true only while this box is ticked. Leaving it unticked submits without the flag, and the backend answers with its own LIVE_KIND_CONFIRMATION_REQUIRED refusal."
          : "Only applies when the target kind is live. Ignored by the backend for a paper broker, and never sent from here."}
      </p>
    </div>
  );
}

export function CreateBrokerForm() {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<BrokerKind>("paper");
  const [provider, setProvider] = useState("");
  const [isActive, setIsActive] = useState(false);
  const [confirmLive, setConfirmLive] = useState(false);

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BrokerResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  /** Any change of target kind invalidates a previous confirmation. */
  function changeKind(next: BrokerKind) {
    setKind(next);
    setConfirmLive(false);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    // confirm_live is added only for a live target that the operator
    // explicitly confirmed. For a paper broker the key is omitted
    // entirely rather than sent as false, so the payload can never carry
    // a live-trading affirmation the user did not make.
    const body: Record<string, unknown> = {
      name,
      kind,
      provider,
      is_active: isActive,
    };
    if (kind === "live" && confirmLive) body.confirm_live = true;

    try {
      const res = await fetch("/api/admin/brokers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as BrokerResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D031).
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="flex flex-col overflow-hidden rounded-lg border border-line bg-surface p-4 shadow-[var(--shadow-panel)]">
      <h3 className="mb-3 text-sm font-semibold tracking-tight text-ink">
        Create broker
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Name
            <input
              className={inputClass}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. longport-paper"
              maxLength={64}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Provider
            <input
              className={inputClass}
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder="e.g. longport"
              maxLength={64}
              required
            />
          </label>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Kind
            <select
              className={inputClass}
              value={kind}
              onChange={(e) => changeKind(e.target.value as BrokerKind)}
            >
              <option value="paper">paper</option>
              <option value="live">live</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={isActive}
              onChange={(e) => setIsActive(e.target.checked)}
            />
            Active
          </label>
        </div>

        <LiveConfirmation
          kind={kind}
          checked={confirmLive}
          onChange={setConfirmLive}
          idPrefix="create-broker"
        />

        <button type="submit" disabled={loading} className={submitClass}>
          {loading ? "Creating…" : "Create broker"}
        </button>
      </form>
      <ResultOrError status={status} errorDetail={errorDetail} result={result} />
    </section>
  );
}

export function ChangeBrokerModeForm() {
  const [brokerId, setBrokerId] = useState("");
  const [kind, setKind] = useState<BrokerKind>("paper");
  const [confirmLive, setConfirmLive] = useState(false);

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BrokerResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  function changeKind(next: BrokerKind) {
    setKind(next);
    setConfirmLive(false);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const body: Record<string, unknown> = { kind };
    if (kind === "live" && confirmLive) body.confirm_live = true;

    try {
      const res = await fetch(
        `/api/admin/brokers/${encodeURIComponent(brokerId)}/mode`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const data = (await res.json().catch(() => null)) as BrokerResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D031).
        if (handleExpiredSession(res.status)) return;
        // The two refusals this endpoint exists to make — the 409 for a
        // broker that already has recorded orders or a simulated book,
        // and the 400 for a missing confirm_live — are shown exactly as
        // the backend worded them. Both describe an audit-integrity rule,
        // and a shorter local paraphrase would drop the reason the
        // operator needs in order to act (create a new broker instead).
        setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="flex flex-col overflow-hidden rounded-lg border border-line bg-surface p-4 shadow-[var(--shadow-panel)]">
      <h3 className="mb-3 text-sm font-semibold tracking-tight text-ink">
        Change broker mode
      </h3>
      <p className="mb-3 text-xs leading-relaxed text-ink-faint">
        Flips one broker between paper and live execution. The backend refuses
        with a 409 once the broker has any recorded order or a simulated
        cash/position book — that history records no per-order kind, so flipping
        it would make simulated and real trades indistinguishable. Create a new
        broker instead.
      </p>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Broker ID
          <input
            className={inputClass}
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="broker UUID"
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Target kind
          <select
            className={inputClass}
            value={kind}
            onChange={(e) => changeKind(e.target.value as BrokerKind)}
          >
            <option value="paper">paper</option>
            <option value="live">live</option>
          </select>
        </label>

        <LiveConfirmation
          kind={kind}
          checked={confirmLive}
          onChange={setConfirmLive}
          idPrefix="change-mode"
        />

        <button type="submit" disabled={loading} className={submitClass}>
          {loading ? "Applying…" : "Change mode"}
        </button>
      </form>
      <ResultOrError status={status} errorDetail={errorDetail} result={result} />
    </section>
  );
}
