import { Fragment } from "react";
import {
  EmptyNote,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type {
  SignalDecision,
  SignalEvaluationResponse,
} from "@/components/CheckSignalsForm";

/** buy -> pos (green), sell -> neg (red), hold -> neutral (grey). */
function signalTone(signal: SignalDecision): "pos" | "neg" | "neutral" {
  if (signal === "buy") return "pos";
  if (signal === "sell") return "neg";
  return "neutral";
}

/** A numeric field is `null` when the backend never computed it — render an
 * em-dash, never `0`. Mirrors `BacktestRunList.pctText`. */
function orDash(raw: string | null): string {
  return raw == null ? "—" : raw;
}

/**
 * `entry_rule_held` / `exit_rule_held` are three-state: the rule held
 * (`true`), it did not (`false`), or there wasn't enough data to evaluate
 * it at all (`null`). Each is rendered as an explicit phrase — a bare blank
 * or an em-dash would blur "no" and "could not evaluate" together, and the
 * master spec forbids a signal whose reasoning is ambiguous.
 */
function ruleHeldText(held: boolean | null): string {
  if (held === true) return "yes";
  if (held === false) return "no";
  return "could not evaluate";
}

/**
 * Renders a list of `SignalEvaluationResponse` as a table — one row per
 * symbol, each with the current signal and its plain-language
 * `explanation` (Phase 61).
 *
 * NOT a client component (no hooks), matching `BacktestTradeLedger`. Each
 * row's indicator values and entry/exit rule checks live in a native
 * `<details>` disclosure in a second, full-width `<tr>`; the `<summary>` is
 * the ghost toggle. No state, no effects.
 *
 * An `insufficient_data: true` row is a real, kept result, never an error:
 * its signal is always HOLD, its close and indicator values show `—`, and
 * its `explanation` (rendered verbatim, in full, never truncated) says how
 * many bars were available versus needed. A small "insufficient data" note
 * sits under its HOLD pill.
 */
export function SignalTable({
  evaluations,
}: {
  evaluations: SignalEvaluationResponse[];
}) {
  return (
    <Panel
      title="Current signals"
      description="The latest evaluation per symbol, exactly as the backend returned it. Expand a row for its indicator values and entry/exit rule checks."
    >
      {evaluations.length === 0 ? (
        <EmptyNote>
          No signal evaluations yet for this version — check some symbols above.
        </EmptyNote>
      ) : (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              Current signal per symbol, exactly as returned by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>Symbol</th>
                <th className={thClass}>Signal</th>
                <th className={thClass}>As of</th>
                <th className={thClass}>Close</th>
                <th className={`${thClass} w-full`}>Explanation</th>
              </tr>
            </thead>
            <tbody>
              {evaluations.map((ev) => {
                const indicatorEntries = Object.entries(ev.indicator_values);
                return (
                  <Fragment key={ev.id}>
                    <tr className={tbodyRowClass} data-testid="signal-row">
                      <td className={`${tdClass} font-semibold`}>{ev.symbol}</td>
                      <td className={tdClass}>
                        <span className="flex flex-col items-start gap-1">
                          <Pill
                            tone={signalTone(ev.signal)}
                            testId={`signal-pill-${ev.symbol}`}
                          >
                            {ev.signal}
                          </Pill>
                          {ev.insufficient_data && (
                            <span
                              className="font-mono text-[10px] uppercase tracking-wide text-ink-faint"
                              data-testid={`insufficient-${ev.symbol}`}
                            >
                              insufficient data
                            </span>
                          )}
                        </span>
                      </td>
                      <td className={`${tdClass} text-ink-muted`}>
                        {ev.as_of_bar_date ?? "—"}
                      </td>
                      <td
                        className={tdClass}
                        data-testid={`close-${ev.symbol}`}
                      >
                        {orDash(ev.latest_close)}
                      </td>
                      <td
                        className={`${tdClass} whitespace-normal font-sans text-ink`}
                        data-testid={`explanation-${ev.symbol}`}
                      >
                        {ev.explanation}
                      </td>
                    </tr>
                    <tr
                      className={tbodyRowClass}
                      data-testid={`signal-detail-row-${ev.symbol}`}
                    >
                      <td className={`${tdClass} whitespace-normal`} colSpan={5}>
                        <details data-testid={`signal-details-${ev.symbol}`}>
                          <summary
                            className={`${btnGhost} list-none [&::-webkit-details-marker]:hidden`}
                            data-testid={`signal-toggle-${ev.symbol}`}
                          >
                            Indicator values &amp; rule checks
                          </summary>
                          <div
                            className="mt-2 flex flex-col gap-2"
                            data-testid={`signal-detail-${ev.symbol}`}
                          >
                            <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1 text-xs">
                              {indicatorEntries.length === 0 ? (
                                <p className="col-span-2 text-ink-muted">
                                  No indicator values on this evaluation.
                                </p>
                              ) : (
                                indicatorEntries.map(([name, value]) => (
                                  <Fragment key={name}>
                                    <dt className="font-mono text-[11px] uppercase tracking-wide text-ink-faint">
                                      {name}
                                    </dt>
                                    <dd
                                      className="tnum font-mono text-[13px] text-ink"
                                      data-testid={`indicator-${ev.symbol}-${name}`}
                                    >
                                      {orDash(value)}
                                    </dd>
                                  </Fragment>
                                ))
                              )}
                            </dl>
                            <ul className="flex flex-col gap-0.5 text-xs text-ink-muted">
                              <li data-testid={`entry-held-${ev.symbol}`}>
                                <span className="font-semibold text-ink">
                                  entry_rule_held
                                </span>
                                {": "}
                                {ruleHeldText(ev.entry_rule_held)}
                              </li>
                              <li data-testid={`exit-held-${ev.symbol}`}>
                                <span className="font-semibold text-ink">
                                  exit_rule_held
                                </span>
                                {": "}
                                {ruleHeldText(ev.exit_rule_held)}
                              </li>
                            </ul>
                          </div>
                        </details>
                      </td>
                    </tr>
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}

export default SignalTable;
