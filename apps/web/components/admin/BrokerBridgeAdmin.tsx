"use client";

import { useMemo, useState } from "react";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Panel,
  Pill,
  btnGhost,
  btnPrimary,
  btnSecondary,
  inputClass,
} from "@/components/ui/primitives";

/**
 * The broker bridge (Phase 90, D109).
 *
 * Two panels. The catalogue says what each venue trades and whether this
 * build can actually reach it; the credential form writes one broker's
 * API keys.
 *
 * Three things this UI refuses to do, each because the convenient version
 * quietly misleads:
 *
 * **It never shows a secret, not even a prefix.** A field that is set
 * renders as "set", and the input is empty. Showing `sk-…4f2` would be a
 * partial disclosure of a live trading credential in exchange for
 * nothing — the operator already knows what they typed.
 *
 * **It marks a catalogued provider as catalogued.** A capability list for
 * a venue with no adapter is a description of the broker, not a promise
 * about this platform, and a form that looked identical either way would
 * let someone wire credentials and expect orders to flow.
 *
 * **It sends the whole set or nothing.** The backend replaces rather than
 * merges, so the form submits every field together; a partial save would
 * leave a stale secret beside a new one and produce a credential set that
 * was never entered as a whole.
 */

type ProviderField = {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  help: string;
};

type Provider = {
  provider: string;
  display_name: string;
  asset_classes: string[];
  order_types: string[];
  supports_short: boolean;
  supports_extended_hours: boolean;
  supports_fractional: boolean;
  supports_cancel: boolean;
  adapter_status: string;
  credential_fields: ProviderField[];
  notes: string;
};

type FieldStatus = ProviderField & { present: boolean; value: string | null };

type CredentialStatus = {
  broker_id: string;
  provider: string;
  configured: boolean;
  fields: FieldStatus[];
  updated_at: string | null;
  key_matches: boolean;
  unreadable_reason: string | null;
};

type BrokerRow = { id: string; name: string; kind: string; provider: string };

export function BrokerCatalogue() {
  const classify = useMemo(
    () =>
      classifyWithAbsences<{ providers: Provider[]; note: string }>(
        [404],
        "No provider catalogue.",
      ),
    [],
  );
  const { data, error } = useKeyedFetch<{ providers: Provider[]; note: string }>({
    key: "providers",
    url: "/api/brokers/providers",
    classify,
  });

  return (
    <Panel
      title="Supported brokers"
      description="What each venue trades, and whether this build can reach it."
    >
      {error ? <Alert>{error}</Alert> : null}
      {!data ? <EmptyNote>Loading…</EmptyNote> : null}
      {data ? (
        <div className="flex flex-col gap-3">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-[11px]">
              <thead>
                <tr className="border-b border-line uppercase tracking-[0.08em] text-ink-faint">
                  <th className="px-2 py-1 font-semibold">broker</th>
                  <th className="px-2 py-1 font-semibold">trades</th>
                  <th className="px-2 py-1 font-semibold">short</th>
                  <th className="px-2 py-1 font-semibold">ext. hours</th>
                  <th className="px-2 py-1 font-semibold">fractional</th>
                  <th className="px-2 py-1 font-semibold">adapter</th>
                </tr>
              </thead>
              <tbody>
                {data.providers.map((p) => (
                  <tr key={p.provider} className="border-b border-line/60 align-top last:border-b-0">
                    <td className="px-2 py-1.5">
                      <div className="font-medium">{p.display_name}</div>
                      <p className="mt-0.5 max-w-[46ch] leading-snug text-ink-faint">{p.notes}</p>
                    </td>
                    <td className="px-2 py-1.5 font-mono">{p.asset_classes.join(", ")}</td>
                    <td className="px-2 py-1.5">{p.supports_short ? "yes" : "no"}</td>
                    <td className="px-2 py-1.5">{p.supports_extended_hours ? "yes" : "no"}</td>
                    <td className="px-2 py-1.5">{p.supports_fractional ? "yes" : "no"}</td>
                    <td className="px-2 py-1.5">
                      <Pill tone={p.adapter_status === "implemented" ? "pos" : "neutral"}>
                        {p.adapter_status}
                      </Pill>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] leading-relaxed text-ink-faint">{data.note}</p>
        </div>
      ) : null}
    </Panel>
  );
}

export function BrokerCredentialsAdmin() {
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  const classifyBrokers = useMemo(
    () => classifyWithAbsences<{ brokers: BrokerRow[] }>([404], "No brokers."),
    [],
  );
  const { data: brokersPayload } = useKeyedFetch<{ brokers: BrokerRow[] }>({
    key: "bridge-brokers",
    url: "/api/brokers",
    classify: classifyBrokers,
  });
  const brokers = brokersPayload?.brokers ?? [];
  const brokerId = (selected && brokers.some((b) => b.id === selected) ? selected : null)
    ?? brokers[0]?.id
    ?? null;
  const broker = brokers.find((b) => b.id === brokerId) ?? null;

  const classifyStatus = useMemo(
    () => classifyWithAbsences<CredentialStatus>([403, 404], "Not visible to this account."),
    [],
  );
  const { data: status, unavailable, error: statusError } = useKeyedFetch<CredentialStatus>({
    key: `cred:${brokerId ?? "none"}:${reload}`,
    url: brokerId ? `/api/brokers/${brokerId}/credentials` : "",
    classify: classifyStatus,
    enabled: brokerId !== null,
  });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!brokerId) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const res = await fetch(`/api/brokers/${brokerId}/credentials`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: draft }),
      });
      const body = (await res.json().catch(() => null)) as { detail?: string } | null;
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError(body?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      // The inputs are cleared on success and never repopulated: the
      // stored secret is not readable, so leaving the typed value on
      // screen would be the only place it still exists.
      setDraft({});
      setSaved("Stored. Secrets are encrypted at rest and are not readable from any route.");
      setReload((n) => n + 1);
    } finally {
      setBusy(false);
    }
  }

  async function forget() {
    if (!brokerId) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const res = await fetch(`/api/brokers/${brokerId}/credentials`, { method: "DELETE" });
      if (handleExpiredSession(res.status)) return;
      if (!res.ok && res.status !== 204) {
        setError(`Request failed (HTTP ${res.status})`);
        return;
      }
      setDraft({});
      setSaved("Forgotten.");
      setReload((n) => n + 1);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Broker credentials"
      description="Write-only. A stored secret is never returned by any route, including this one."
    >
      {brokers.length === 0 ? (
        <EmptyNote>
          No broker is granted to this account yet. Create one and grant it under Broker grants
          first.
        </EmptyNote>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-1" role="group" aria-label="Broker">
            {brokers.map((b) => (
              <button
                key={b.id}
                type="button"
                onClick={() => {
                  setSelected(b.id);
                  setDraft({});
                  setSaved(null);
                  setError(null);
                }}
                aria-pressed={b.id === brokerId}
                className={b.id === brokerId ? btnSecondary : btnGhost}
              >
                {b.name} <span className="font-mono text-[10px]">{b.provider}</span>
              </button>
            ))}
          </div>

          {statusError ? <Alert>{statusError}</Alert> : null}
          {unavailable && !status ? <EmptyNote>{unavailable}</EmptyNote> : null}

          {status ? (
            <form className="flex flex-col gap-3" onSubmit={submit}>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Pill tone={status.configured ? "pos" : "neutral"}>
                  {status.configured ? "configured" : "not configured"}
                </Pill>
                <span className="font-mono text-ink-faint">{broker?.provider}</span>
                {status.updated_at ? (
                  <span className="text-ink-faint">
                    last written {new Date(status.updated_at).toLocaleString()}
                  </span>
                ) : null}
              </div>

              {status.unreadable_reason ? <Alert>{status.unreadable_reason}</Alert> : null}

              {status.fields.length === 0 ? (
                <EmptyNote>
                  This provider needs no credentials — the paper broker is in-process and
                  reaches no venue.
                </EmptyNote>
              ) : (
                <div className="grid gap-3 md:grid-cols-2">
                  {status.fields.map((f) => (
                    <label key={f.name} className="flex flex-col gap-1 text-xs">
                      <span className="flex items-center gap-2 font-medium">
                        {f.label}
                        {f.required ? null : (
                          <span className="text-ink-faint">(optional)</span>
                        )}
                        <Pill tone={f.present ? "pos" : "neutral"}>
                          {f.present ? "set" : "not set"}
                        </Pill>
                      </span>
                      <input
                        id={`cred-${f.name}`}
                        type={f.secret ? "password" : "text"}
                        autoComplete="off"
                        className={`${inputClass} font-mono`}
                        // A non-secret keeps its stored value visible so an
                        // operator can confirm WHICH account is wired; a
                        // secret's box is always empty.
                        value={draft[f.name] ?? (f.secret ? "" : (f.value ?? ""))}
                        placeholder={f.secret && f.present ? "stored — type to replace" : ""}
                        onChange={(e) =>
                          setDraft((d) => ({ ...d, [f.name]: e.target.value }))
                        }
                      />
                      <span className="leading-snug text-ink-faint">{f.help}</span>
                    </label>
                  ))}
                </div>
              )}

              {error ? <Alert>{error}</Alert> : null}
              {saved ? <p className="text-xs text-pos">{saved}</p> : null}

              {status.fields.length > 0 ? (
                <div className="flex flex-wrap items-center gap-2">
                  <button type="submit" className={btnPrimary} disabled={busy}>
                    {busy ? "Saving…" : "Save credentials"}
                  </button>
                  <button
                    type="button"
                    className={btnGhost}
                    onClick={forget}
                    disabled={busy || !status.configured}
                  >
                    Forget
                  </button>
                  <p className="text-[11px] leading-relaxed text-ink-faint">
                    Every field is sent together — the store replaces rather than merges, so a
                    half-filled form would otherwise leave a stale secret beside a new one.
                  </p>
                </div>
              ) : null}
            </form>
          ) : null}
        </div>
      )}
    </Panel>
  );
}
