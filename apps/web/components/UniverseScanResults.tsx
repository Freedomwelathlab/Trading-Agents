import Link from "next/link";
import {
  Alert,
  KeyValue,
  Panel,
  Pill,
  Stat,
  TableScroll,
  Term,
  Value,
  btnGhost,
  hintClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type {
  UniverseScanDetailResponse,
  UniverseScanResultResponse,
} from "@/components/UniverseScanForm";

function statusTone(status: string): "pos" | "neg" | "warn" | "neutral" {
  if (status === "succeeded") return "pos";
  if (status === "failed") return "neg";
  if (status === "running") return "warn";
  return "neutral";
}

/** A metric is `null`, never `0`, on a failed per-symbol result — render an
 * em-dash. Mirrors `BacktestRunList.pctText`. */
function pctText(raw: string | null): string {
  return raw == null ? "—" : raw;
}

/**
 * Colours a percentage from the sign of the number the backend returned.
 * A per-symbol total return IS a signed P&L figure, so this colouring is
 * appropriate here. Mirrors `PortfolioView.pnlTone` / `BacktestPanel.returnTone`.
 */
function pctClass(raw: string | null): string {
  if (raw == null) return "";
  const n = Number(raw);
  if (!Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "text-pos" : "text-neg";
}

/** A plain-language read of the three roll-up counts. */
function summaryLine(scan: UniverseScanDetailResponse): string {
  const total = scan.num_symbols;
  const ok = scan.num_succeeded;
  const qual = scan.num_qualified;
  if (total == null || ok == null) {
    return "This scan has not finished yet.";
  }
  const sym = (n: number) => `${n} symbol${n === 1 ? "" : "s"}`;
  if (ok === 0) {
    return `None of the ${sym(total)} had enough history for this window.`;
  }
  const first = `${ok} of ${sym(total)} had enough data`;
  if (qual == null) return `${first}.`;
  return `${first}; ${qual} ${qual === 1 ? "was" : "were"} profitable over this window.`;
}

/**
 * One universe scan's ranked results (Phase 60).
 *
 * `strategyId` comes from the page URL (the scan shape itself carries only
 * `strategy_version_id`); it is needed so each row can link to that
 * symbol's full Phase 56 backtest-run detail page.
 *
 * A `"failed"` scan carries `error_detail` and no usable results — that is
 * shown as an error `Alert` and nothing else. A `"succeeded"` scan with
 * `num_succeeded: 0` is NOT an error: it means no symbol had enough
 * history for this window, and the results table still lists every symbol
 * (all failed, each with its own `error_detail`) under an honest note.
 * Every metric on a failed row is `—`, never `0%`.
 */
export function UniverseScanResults({
  scan,
  strategyId,
}: {
  scan: UniverseScanDetailResponse;
  strategyId: string;
}) {
  return (
    <div className="flex flex-col gap-5">
      <Panel
        title="Scan"
        actions={<Pill tone={statusTone(scan.status)}>{scan.status}</Pill>}
      >
        <KeyValue>
          <Term>strategy_version_id</Term>
          <Value testId="scan-version">{scan.strategy_version_id}</Value>
          <Term>scan_mode</Term>
          <Value>{scan.scan_mode}</Value>
          <Term>start_date</Term>
          <Value>{scan.start_date}</Value>
          <Term>end_date</Term>
          <Value>{scan.end_date}</Value>
          <Term>starting_cash</Term>
          <Value>{scan.starting_cash}</Value>
          <Term>created_at</Term>
          <Value>{scan.created_at}</Value>
          <Term>completed_at</Term>
          <Value>{scan.completed_at ?? "—"}</Value>
        </KeyValue>

        <div className="grid gap-2 sm:grid-cols-3">
          <Stat
            label="num_symbols"
            value={scan.num_symbols ?? "—"}
            testId="scan-num-symbols"
          />
          <Stat
            label="num_succeeded"
            value={scan.num_succeeded ?? "—"}
            testId="scan-num-succeeded"
          />
          <Stat
            label="num_qualified"
            value={scan.num_qualified ?? "—"}
            tone={scan.num_qualified != null && scan.num_qualified > 0 ? "pos" : undefined}
            testId="scan-num-qualified"
          />
        </div>

        <p className={hintClass} data-testid="scan-summary">
          {summaryLine(scan)}
        </p>
      </Panel>

      {scan.status === "failed" ? (
        <Alert tone="error" testId="scan-error-detail">
          {scan.error_detail ??
            "This scan failed, but the backend returned no error_detail."}
        </Alert>
      ) : (
        <Panel
          title="Ranked results"
          description="Every scanned symbol, best-first by return, exactly as the backend ordered them. Failed symbols (no ingested bars over the window) sit at the bottom with an em-dash for every metric."
        >
          {scan.results.length === 0 ? (
            <p className={hintClass}>This scan produced no per-symbol results.</p>
          ) : (
            <TableScroll>
              <table className={tableClass}>
                <caption className="sr-only">
                  Ranked per-symbol results for this universe scan, exactly as
                  returned by the API
                </caption>
                <thead>
                  <tr className={theadRowClass}>
                    <th className={thClass}>Rank</th>
                    <th className={thClass}>Symbol</th>
                    <th className={thClass}>total_return_pct</th>
                    <th className={thClass}>max_drawdown_pct</th>
                    <th className={thClass}>win_rate_pct</th>
                    <th className={thClass}>num_trades</th>
                    <th className={thClass}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {scan.results.map((r: UniverseScanResultResponse) => (
                    <tr
                      key={r.id}
                      className={tbodyRowClass}
                      data-testid="universe-scan-result-row"
                    >
                      <td className={tdClass} data-testid={`rank-${r.symbol}`}>
                        {r.rank != null ? `#${r.rank}` : "—"}
                      </td>
                      <td className={`${tdClass} font-semibold`}>
                        {r.symbol}
                        {r.status === "failed" && r.error_detail && (
                          <span
                            className="mt-0.5 block font-mono text-[11px] font-normal text-ink-faint"
                            data-testid={`result-error-${r.symbol}`}
                          >
                            {r.error_detail}
                          </span>
                        )}
                      </td>
                      <td
                        className={`${tdClass} ${pctClass(r.total_return_pct)}`}
                        data-testid={`return-${r.symbol}`}
                      >
                        {pctText(r.total_return_pct)}
                      </td>
                      <td className={tdClass} data-testid={`drawdown-${r.symbol}`}>
                        {pctText(r.max_drawdown_pct)}
                      </td>
                      <td className={tdClass} data-testid={`winrate-${r.symbol}`}>
                        {pctText(r.win_rate_pct)}
                      </td>
                      <td className={tdClass} data-testid={`trades-${r.symbol}`}>
                        {r.num_trades ?? "—"}
                      </td>
                      <td className={tdClass}>
                        {r.backtest_run_id ? (
                          <Link
                            className={btnGhost}
                            href={`/strategies/${strategyId}/backtests/${r.backtest_run_id}`}
                          >
                            Backtest
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
        </Panel>
      )}
    </div>
  );
}

export default UniverseScanResults;
