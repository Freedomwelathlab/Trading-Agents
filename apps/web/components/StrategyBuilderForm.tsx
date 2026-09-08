"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  Field,
  Panel,
  btnGhost,
  btnPrimary,
  btnSecondary,
  hintClass,
  inputClass,
} from "@/components/ui/primitives";

/* ------------------------------------------------------------------ */
/* StrategyDefinition shape (the frozen backend contract, Phase 54)    */
/* ------------------------------------------------------------------ */

export type Indicator = { id: string; type: "sma" | "rsi"; period: number };

export type RuleOp = "crosses_above" | "crosses_below" | "gt" | "gte" | "lt" | "lte";

/** A string operand is either the literal `"close"` or a declared
 * indicator's `id`; a number operand is a fixed threshold. */
export type Operand = string | number;

export type Rule = { op: RuleOp; left: Operand; right: Operand };

export type PositionSizing =
  | { type: "all_in" }
  | { type: "fixed_fraction"; fraction: number }
  | { type: "fixed_notional"; amount: number };

export type StrategyDefinition = {
  indicators: Indicator[];
  entry_rule: Rule;
  exit_rule: Rule;
  position_sizing: PositionSizing;
};

export type StrategyVersionResponse = {
  id: string;
  strategy_id: string;
  version_number: number;
  definition: StrategyDefinition;
  definition_hash: string;
  status: "draft" | "validated" | "archived";
  created_by_user_id: string | null;
  created_at: string;
  validated_at: string | null;
};

const RULE_OPS: RuleOp[] = ["crosses_above", "crosses_below", "gt", "gte", "lt", "lte"];

/**
 * FastAPI's default 422 shape is a list of `{loc, msg}` objects; the
 * backend's own `/validate` shape is `{"detail": {"errors": string[]}}`; a
 * 409 (`VERSION_NOT_DRAFT: ...`) carries a plain string. This normalizes
 * all three into a list of lines to render, never collapsing them into one
 * invented message (mirrors `BacktestPanel.formatDetail`, adapted so the
 * `{errors: [...]}` shape keeps every string as its own line).
 */
export function formatDetailLines(detail: unknown): string[] {
  if (detail == null) return [];
  if (typeof detail === "string") return [detail];
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (item && typeof item === "object") {
        const rec = item as Record<string, unknown>;
        const loc = Array.isArray(rec.loc) ? rec.loc.join(".") : undefined;
        const msg = typeof rec.msg === "string" ? rec.msg : JSON.stringify(item);
        return loc ? `${loc}: ${msg}` : msg;
      }
      return String(item);
    });
  }
  if (typeof detail === "object") {
    const rec = detail as Record<string, unknown>;
    if (Array.isArray(rec.errors)) {
      return rec.errors.map((e) => String(e));
    }
  }
  return [JSON.stringify(detail)];
}

/**
 * Renders every line as its own visible row inside one `Alert`. `Alert`'s
 * root element is a `<p>` (phrasing content only), so this deliberately
 * joins lines with `<br/>` rather than a `<ul>`/`<li>` list — a block
 * element nested in a `<p>` would parse back differently than React's
 * virtual DOM describes it once Next.js serializes this to real HTML,
 * producing a hydration mismatch. `<br/>` keeps every string its own line
 * without leaving valid HTML.
 */
function ErrorLines({ lines }: { lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <Alert tone="error">
      {lines.map((line, i) => (
        <span key={i}>
          {i > 0 && <br />}
          {line}
        </span>
      ))}
    </Alert>
  );
}

/* ------------------------------------------------------------------ */
/* Operand input                                                       */
/* ------------------------------------------------------------------ */

const NUMBER_OPTION = "__number__";

/**
 * Renders an operand as either a select (the literal `"close"`, a declared
 * indicator id, or the operand's own current value if it refers to an id
 * that has since been removed — never silently dropped) or, when "Fixed
 * number" is chosen, a numeric input. Always produces a syntactically valid
 * operand: `"close"`, a declared indicator id, or a number.
 */
function OperandField({
  label,
  value,
  onChange,
  indicatorIds,
  disabled,
}: {
  label: string;
  value: Operand;
  onChange: (v: Operand) => void;
  indicatorIds: string[];
  disabled: boolean;
}) {
  const isNumber = typeof value === "number";
  const staleId =
    !isNumber && value !== "close" && !indicatorIds.includes(value) ? value : null;
  const options = ["close", ...indicatorIds, ...(staleId ? [staleId] : [])];
  const selectValue = isNumber ? NUMBER_OPTION : value;

  return (
    <div className="flex flex-col gap-1.5">
      <Field label={label}>
        <select
          className={inputClass}
          value={selectValue}
          disabled={disabled}
          onChange={(e) => {
            const v = e.target.value;
            onChange(v === NUMBER_OPTION ? 0 : v);
          }}
        >
          {options.map((opt) => (
            <option key={opt} value={opt}>
              {opt === "close" ? "close (literal)" : opt}
            </option>
          ))}
          <option value={NUMBER_OPTION}>Fixed number…</option>
        </select>
      </Field>
      {isNumber && (
        <input
          type="number"
          aria-label={`${label} threshold`}
          className={inputClass}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(Number(e.target.value))}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Builder                                                              */
/* ------------------------------------------------------------------ */

/**
 * Form-based rule composer for a `StrategyVersion`'s `definition` (Phase
 * 54) — deliberately not a raw JSON textarea and not a visual node/flow
 * editor, per the approved plan's scoped v1.
 *
 * Read-only whenever the version isn't a draft: every control is disabled
 * and a note explains why, rather than only relying on the backend's 409
 * (defense in depth, and better UX than waiting for the round trip).
 *
 * The caller should remount this component (e.g. `key={version.id}`) when
 * the selected version changes — its edit state is local and derived once
 * from `version.definition` on mount.
 */
export default function StrategyBuilderForm({
  strategyId,
  version,
  onVersionUpdate,
}: {
  strategyId: string;
  version: StrategyVersionResponse;
  onVersionUpdate?: (updated: StrategyVersionResponse) => void;
}) {
  const readOnly = version.status !== "draft";

  const [indicators, setIndicators] = useState<Indicator[]>(version.definition.indicators ?? []);
  const [entryRule, setEntryRule] = useState<Rule>(
    version.definition.entry_rule ?? { op: "gt", left: "close", right: 0 },
  );
  const [exitRule, setExitRule] = useState<Rule>(
    version.definition.exit_rule ?? { op: "lt", left: "close", right: 0 },
  );
  const [positionSizing, setPositionSizing] = useState<PositionSizing>(
    version.definition.position_sizing ?? { type: "all_in" },
  );

  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<number | null>(null);
  const [saveErrorLines, setSaveErrorLines] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);

  const [validating, setValidating] = useState(false);
  const [validateStatus, setValidateStatus] = useState<number | null>(null);
  const [validateErrorLines, setValidateErrorLines] = useState<string[]>([]);
  const [validatedResultStatus, setValidatedResultStatus] = useState<string | null>(null);

  const indicatorIds = indicators.map((i) => i.id).filter((id) => id !== "");

  function assembleDefinition(): StrategyDefinition {
    return {
      indicators,
      entry_rule: entryRule,
      exit_rule: exitRule,
      position_sizing: positionSizing,
    };
  }

  function addIndicator() {
    setIndicators((prev) => [...prev, { id: `ind_${prev.length + 1}`, type: "sma", period: 20 }]);
  }

  function updateIndicator(index: number, patch: Partial<Indicator>) {
    setIndicators((prev) => prev.map((ind, i) => (i === index ? { ...ind, ...patch } : ind)));
  }

  function removeIndicator(index: number) {
    setIndicators((prev) => prev.filter((_, i) => i !== index));
  }

  async function handleSave() {
    setSaving(true);
    setSaveErrorLines([]);
    setSaveStatus(null);
    setSaved(false);
    try {
      const res = await fetch(`/api/strategies/${strategyId}/versions/${version.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ definition: assembleDefinition() }),
      });
      const data = (await res.json().catch(() => null)) as
        | (Partial<StrategyVersionResponse> & { detail?: unknown })
        | null;
      setSaveStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setSaveErrorLines(formatDetailLines(data?.detail));
        return;
      }
      setSaved(true);
      if (data) onVersionUpdate?.(data as StrategyVersionResponse);
    } catch {
      setSaveErrorLines(["DATA_UNAVAILABLE: could not reach the trading API"]);
    } finally {
      setSaving(false);
    }
  }

  async function handleValidate() {
    setValidating(true);
    setValidateErrorLines([]);
    setValidateStatus(null);
    setValidatedResultStatus(null);
    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${version.id}/validate`,
        { method: "POST" },
      );
      const data = (await res.json().catch(() => null)) as
        | (Partial<StrategyVersionResponse> & { detail?: unknown })
        | null;
      setValidateStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setValidateErrorLines(formatDetailLines(data?.detail));
        return;
      }
      setValidatedResultStatus(data?.status ?? null);
      if (data) onVersionUpdate?.(data as StrategyVersionResponse);
    } catch {
      setValidateErrorLines(["DATA_UNAVAILABLE: could not reach the trading API"]);
    } finally {
      setValidating(false);
    }
  }

  return (
    <Panel
      title="Strategy builder"
      description="Assembles the StrategyDefinition JSON this version validates and runs against. Structural validation only — no execution."
    >
      {readOnly && (
        <Alert tone="warn" role="status">
          This version is {version.status} and can no longer be edited — fork a new
          draft to make changes.
        </Alert>
      )}

      <div className="flex flex-col gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
          Indicators
        </h3>
        <div className="flex flex-col gap-2">
          {indicators.map((ind, i) => (
            <div
              key={i}
              className="grid gap-2 rounded-md border border-line bg-well p-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
              data-testid={`indicator-row-${i}`}
            >
              <Field label="ID">
                <input
                  className={inputClass}
                  value={ind.id}
                  disabled={readOnly}
                  onChange={(e) => updateIndicator(i, { id: e.target.value })}
                />
              </Field>
              <Field label="Type">
                <select
                  className={inputClass}
                  value={ind.type}
                  disabled={readOnly}
                  onChange={(e) =>
                    updateIndicator(i, { type: e.target.value as Indicator["type"] })
                  }
                >
                  <option value="sma">sma</option>
                  <option value="rsi">rsi</option>
                </select>
              </Field>
              <Field label="Period">
                <input
                  type="number"
                  className={inputClass}
                  value={ind.period}
                  disabled={readOnly}
                  onChange={(e) => updateIndicator(i, { period: Number(e.target.value) })}
                />
              </Field>
              <button
                type="button"
                className={btnGhost}
                disabled={readOnly}
                onClick={() => removeIndicator(i)}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
        <button
          type="button"
          className={`${btnSecondary} self-start`}
          disabled={readOnly}
          onClick={addIndicator}
        >
          + Add indicator
        </button>
      </div>

      <div className="flex flex-col gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
          Entry rule
        </h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Entry op">
            <select
              className={inputClass}
              value={entryRule.op}
              disabled={readOnly}
              onChange={(e) => setEntryRule({ ...entryRule, op: e.target.value as RuleOp })}
            >
              {RULE_OPS.map((op) => (
                <option key={op} value={op}>
                  {op}
                </option>
              ))}
            </select>
          </Field>
          <OperandField
            label="Entry left"
            value={entryRule.left}
            indicatorIds={indicatorIds}
            disabled={readOnly}
            onChange={(v) => setEntryRule({ ...entryRule, left: v })}
          />
          <OperandField
            label="Entry right"
            value={entryRule.right}
            indicatorIds={indicatorIds}
            disabled={readOnly}
            onChange={(v) => setEntryRule({ ...entryRule, right: v })}
          />
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
          Exit rule
        </h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Exit op">
            <select
              className={inputClass}
              value={exitRule.op}
              disabled={readOnly}
              onChange={(e) => setExitRule({ ...exitRule, op: e.target.value as RuleOp })}
            >
              {RULE_OPS.map((op) => (
                <option key={op} value={op}>
                  {op}
                </option>
              ))}
            </select>
          </Field>
          <OperandField
            label="Exit left"
            value={exitRule.left}
            indicatorIds={indicatorIds}
            disabled={readOnly}
            onChange={(v) => setExitRule({ ...exitRule, left: v })}
          />
          <OperandField
            label="Exit right"
            value={exitRule.right}
            indicatorIds={indicatorIds}
            disabled={readOnly}
            onChange={(v) => setExitRule({ ...exitRule, right: v })}
          />
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
          Position sizing
        </h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Sizing type">
            <select
              className={inputClass}
              value={positionSizing.type}
              disabled={readOnly}
              onChange={(e) => {
                const t = e.target.value as PositionSizing["type"];
                if (t === "all_in") setPositionSizing({ type: "all_in" });
                else if (t === "fixed_fraction")
                  setPositionSizing({ type: "fixed_fraction", fraction: 1 });
                else setPositionSizing({ type: "fixed_notional", amount: 1000 });
              }}
            >
              <option value="all_in">all_in</option>
              <option value="fixed_fraction">fixed_fraction</option>
              <option value="fixed_notional">fixed_notional</option>
            </select>
          </Field>
          {positionSizing.type === "fixed_fraction" && (
            <Field label="Fraction (0–1]">
              <input
                type="number"
                step="0.01"
                min={0}
                max={1}
                className={inputClass}
                value={positionSizing.fraction}
                disabled={readOnly}
                onChange={(e) =>
                  setPositionSizing({ type: "fixed_fraction", fraction: Number(e.target.value) })
                }
              />
            </Field>
          )}
          {positionSizing.type === "fixed_notional" && (
            <Field label="Amount">
              <input
                type="number"
                min={0}
                className={inputClass}
                value={positionSizing.amount}
                disabled={readOnly}
                onChange={(e) =>
                  setPositionSizing({ type: "fixed_notional", amount: Number(e.target.value) })
                }
              />
            </Field>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
        <button
          type="button"
          className={btnPrimary}
          disabled={readOnly || saving}
          onClick={() => void handleSave()}
        >
          {saving ? "Saving…" : "Save draft"}
        </button>
        <button
          type="button"
          className={btnSecondary}
          disabled={readOnly || validating}
          onClick={() => void handleValidate()}
        >
          {validating ? "Validating…" : "Validate"}
        </button>
        {saved && saveErrorLines.length === 0 && (
          <span className={hintClass}>Saved.</span>
        )}
      </div>

      {saveErrorLines.length > 0 && (
        <>
          {saveStatus && (
            <p className={hintClass}>HTTP {saveStatus}</p>
          )}
          <ErrorLines lines={saveErrorLines} />
        </>
      )}

      {validatedResultStatus && validateErrorLines.length === 0 && (
        <Alert tone="info" role="status">
          Validated — this version&apos;s status is now{" "}
          <span className="font-mono">{validatedResultStatus}</span>.
        </Alert>
      )}

      {validateErrorLines.length > 0 && (
        <>
          {validateStatus && <p className={hintClass}>HTTP {validateStatus}</p>}
          <ErrorLines lines={validateErrorLines} />
        </>
      )}
    </Panel>
  );
}
