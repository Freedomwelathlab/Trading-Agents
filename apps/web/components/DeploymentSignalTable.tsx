import {
  EmptyNote,
  Pill,
  TableScroll,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type {
  DeploymentSignalDecision,
  DeploymentSignalResponse,
} from "@/components/CreateDeploymentForm";

/** buy -> pos (green), sell -> neg (red), hold -> neutral (grey). Mirrors
 * `SignalTable.signalTone`. */
function signalTone(signal: DeploymentSignalDecision): "pos" | "neg" | "neutral" {
  if (signal === "buy") return "pos";
  if (signal === "sell") return "neg";
  return "neutral";
}

/** A numeric field is `null` when the backend never computed it — render an
 * em-dash, never `0`. */
function orDash(raw: string | null): string {
  return raw == null ? "—" : raw;
}

/**
 * One deployment's signal trail (Phase 63) — every `SignalEvaluation` its
 * runner cycles produced, newest first, exactly as the backend returned
 * it. The Phase-61 evaluation shape minus the fields a deployment context
 * makes redundant. NOT a client component (no hooks), matching
 * `SignalTable`.
 *
 * An `insufficient_data: true` row is a real, kept result, never an error:
 * its signal is HOLD, its close shows `—`, and its `explanation` (verbatim,
 * never truncated) says how many bars were available versus needed.
 */
export function DeploymentSignalTable({
  signals,
}: {
  signals: DeploymentSignalResponse[];
}) {
  if (signals.length === 0) {
    return (
      <EmptyNote>
        No signals from this deployment yet — a runner cycle persists one per
        symbol each time it evaluates.
      </EmptyNote>
    );
  }
  return (
    <TableScroll>
      <table className={tableClass}>
        <caption className="sr-only">
          Signals produced by this deployment&apos;s runner cycles, exactly as
          returned by the API
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
          {signals.map((sig) => (
            <tr
              key={sig.id}
              className={tbodyRowClass}
              data-testid="deployment-signal-row"
            >
              <td className={`${tdClass} font-semibold`}>{sig.symbol}</td>
              <td className={tdClass}>
                <span className="flex flex-col items-start gap-1">
                  <Pill
                    tone={signalTone(sig.signal)}
                    testId={`deployment-signal-pill-${sig.symbol}`}
                  >
                    {sig.signal}
                  </Pill>
                  {sig.insufficient_data && (
                    <span
                      className="font-mono text-[10px] uppercase tracking-wide text-ink-faint"
                      data-testid={`deployment-insufficient-${sig.symbol}`}
                    >
                      insufficient data
                    </span>
                  )}
                </span>
              </td>
              <td className={`${tdClass} text-ink-muted`}>
                {sig.as_of_bar_date ?? "—"}
              </td>
              <td
                className={tdClass}
                data-testid={`deployment-close-${sig.symbol}`}
              >
                {orDash(sig.latest_close)}
              </td>
              <td
                className={`${tdClass} whitespace-normal font-sans text-ink`}
                data-testid={`deployment-explanation-${sig.symbol}`}
              >
                {sig.explanation}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}

export default DeploymentSignalTable;
