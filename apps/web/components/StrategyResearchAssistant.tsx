"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  Field,
  Panel,
  btnPrimary,
  hintClass,
  inputClass,
} from "@/components/ui/primitives";

const MAX_BRIEF_LENGTH = 500;

/**
 * The shape `POST /strategies/research/propose` returns on success (Phase
 * 67 / D085). `definition` is deliberately left as a loose record rather
 * than typed as `StrategyBuilderForm`'s `StrategyDefinition`: the whole
 * point of `is_valid`/`validation_errors` is that the LLM's draft is NOT
 * guaranteed to conform to that shape, so pretending otherwise here would
 * misrepresent what the backend is actually telling the caller.
 */
export type ProposeResponse = {
  name: string;
  definition: Record<string, unknown>;
  rationale: string;
  is_valid: boolean;
  validation_errors: string[];
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * but a list of objects for a 422 request-shape validation error. Mirrors
 * `StrategyList.formatDetail` / `BacktestPanel.formatDetail`.
 */
export function formatDetail(detail: unknown): string | null {
  if (detail == null) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object") {
          const rec = item as Record<string, unknown>;
          const loc = Array.isArray(rec.loc) ? rec.loc.join(".") : undefined;
          const msg = typeof rec.msg === "string" ? rec.msg : JSON.stringify(item);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

/**
 * AI strategy research assistant (Phase 67 / D085), read-only and
 * advisory only.
 *
 * `POST /strategies/research/propose` never persists anything — there is
 * no "save this draft" endpoint this phase, so this component never
 * invents one. A valid draft is meant to be copied by hand into the
 * Strategy Builder's own indicator / entry-rule / exit-rule /
 * position-sizing fields on a new strategy's draft version (see
 * `app/strategies/[strategyId]/page.tsx` + `StrategyBuilderForm`, which
 * has no raw-JSON-paste field either, per its own docblock).
 *
 * Four distinct backend outcomes, rendered as four distinct states —
 * never collapsed into a single generic error, because collapsing them
 * would hide exactly the information a caller needs:
 *
 * - 200 with `is_valid: true` — the draft is shown as a real, usable
 *   skeleton.
 * - 200 with `is_valid: false` — the SAME draft is shown, but with a
 *   visibly distinct "structural problems" callout listing every string
 *   in `validation_errors` verbatim. This is the important honest-signal
 *   case: it must never look identical to the valid case.
 * - 400 `NOT_CONFIGURED:` — a calm, neutral-toned notice that this
 *   environment has no LLM provider wired. Not the user's fault, so not
 *   styled like one.
 * - 502 (the LLM's own response was unusable) or a thrown fetch error —
 *   a real failure, styled as an error and inviting a retry.
 */
export default function StrategyResearchAssistant() {
  const [brief, setBrief] = useState("");
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<number | null>(null);

  const [validationError, setValidationError] = useState<string | null>(null);
  const [genericError, setGenericError] = useState<string | null>(null);
  const [notConfiguredDetail, setNotConfiguredDetail] = useState<string | null>(null);
  const [failedMessage, setFailedMessage] = useState<string | null>(null);
  const [result, setResult] = useState<ProposeResponse | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setValidationError(null);
    setGenericError(null);
    setNotConfiguredDetail(null);
    setFailedMessage(null);
    setResult(null);
    setStatus(null);

    const trimmed = brief.trim();
    if (trimmed.length === 0) {
      setValidationError("Describe the strategy idea you want to explore before submitting.");
      return;
    }
    if (trimmed.length > MAX_BRIEF_LENGTH) {
      setValidationError(
        `Keep the brief to ${MAX_BRIEF_LENGTH} characters or fewer — it is currently ${trimmed.length}.`,
      );
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/api/strategies/research/propose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brief: trimmed }),
      });
      const data = (await res.json().catch(() => null)) as
        | (Partial<ProposeResponse> & { detail?: unknown })
        | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        const detail = typeof data?.detail === "string" ? data.detail : null;
        if (res.status === 400 && detail?.startsWith("NOT_CONFIGURED")) {
          setNotConfiguredDetail(detail);
          return;
        }
        if (res.status === 502) {
          setFailedMessage(
            detail
              ? `The research assistant's draft could not be used — try again. (${detail})`
              : "The research assistant's draft could not be used — try again.",
          );
          return;
        }
        setGenericError(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      if (data && typeof data.name === "string") {
        setResult(data as ProposeResponse);
      }
    } catch {
      setFailedMessage("Could not reach the research assistant — try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Panel
      title="Propose a strategy draft"
      description="A brief, technical draft for a technical user — this is a starting point for the Strategy Builder, not investment advice."
    >
      <form onSubmit={handleSubmit} className="grid gap-3">
        <Field
          label="Describe the strategy idea you want to explore"
          hint={`${brief.length}/${MAX_BRIEF_LENGTH} characters`}
        >
          <textarea
            className={`${inputClass} min-h-28 resize-y`}
            value={brief}
            maxLength={MAX_BRIEF_LENGTH}
            onChange={(e) => setBrief(e.target.value)}
            placeholder="e.g. A mean-reversion strategy that buys when RSI is oversold and exits when it recovers"
          />
        </Field>

        <div>
          <button type="submit" disabled={loading} className={btnPrimary}>
            {loading ? "Proposing…" : "Propose a draft"}
          </button>
        </div>
      </form>

      {validationError && (
        <Alert tone="warn" testId="research-validation">
          {validationError}
        </Alert>
      )}

      {genericError && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {genericError}
        </Alert>
      )}

      {notConfiguredDetail && (
        <Alert tone="info" role="status" testId="research-not-configured">
          {notConfiguredDetail}
          <span className={`mt-1 block ${hintClass}`}>
            This feature isn&apos;t available in this environment — no LLM
            provider is configured. Everything else in Strategy Lab still
            works.
          </span>
        </Alert>
      )}

      {failedMessage && (
        <Alert tone="error" testId="research-failed">
          {failedMessage}
        </Alert>
      )}

      {result && (
        <div className="flex flex-col gap-4 border-t border-line pt-4" data-testid="research-result">
          <div>
            <h2 className="text-lg font-semibold text-ink">{result.name}</h2>
            <p className="mt-1 text-sm leading-relaxed text-ink-muted">{result.rationale}</p>
          </div>

          {!result.is_valid && (
            <Alert tone="warn" testId="research-invalid-callout">
              <strong>This draft has structural problems:</strong>
              {result.validation_errors.map((err, i) => (
                <span key={i}>
                  <br />
                  {err}
                </span>
              ))}
            </Alert>
          )}

          <div>
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
              Proposed strategy definition
            </h3>
            <pre
              className="mt-2 overflow-x-auto rounded-md border border-line bg-well p-3 font-mono text-[12px] leading-relaxed text-ink"
              data-testid="research-definition"
            >
              {JSON.stringify(result.definition, null, 2)}
            </pre>
          </div>

          {result.is_valid && (
            <p className={hintClass}>
              This draft is structurally valid. There is no &quot;save this
              draft&quot; button here — copy the indicators, entry/exit
              rules, and position sizing above into the Strategy
              Builder&apos;s own fields on a new strategy&apos;s draft
              version (create a strategy from the Strategy Lab list, then
              open it) to turn this into a real, saved version.
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}
