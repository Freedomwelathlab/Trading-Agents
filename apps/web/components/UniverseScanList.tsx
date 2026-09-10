import Link from "next/link";
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
import type { UniverseScanSummary } from "@/components/UniverseScanForm";

function statusTone(status: string): "pos" | "neg" | "warn" | "neutral" {
  if (status === "succeeded") return "pos";
  if (status === "failed") return "neg";
  if (status === "running") return "warn";
  return "neutral";
}

/** A roll-up count is `null`, never `0`, on a scan that failed or is still
 * running — render an em-dash, matching `BacktestRunList.pctText`. */
function countText(n: number | null): string {
  return n == null ? "—" : String(n);
}

/** `explicit_list` shows the symbol count; `all_ingested` says so. */
function modeText(scan: UniverseScanSummary): string {
  if (scan.scan_mode === "all_ingested") return "all ingested";
  const n = scan.requested_symbols?.length ?? scan.num_symbols ?? 0;
  return `${n} symbol${n === 1 ? "" : "s"}`;
}

/**
 * A version's universe-scan history (Phase 60), read-only. `scans` is
 * supplied by the parent page, which owns the fetch — this component never
 * fetches, matching `BacktestRunList`'s split of "who fetches what".
 *
 * A failed scan's counts show as `—` (the backend sends `null`, not `0`);
 * its `error_detail` lives on the detail page the "View" link points to.
 * `num_qualified` gets a subtle positive emphasis when it is above zero,
 * since that is the number a reader is scanning this table for.
 */
export function UniverseScanList({
  strategyId,
  versionId,
  scans,
}: {
  strategyId: string;
  versionId: string;
  scans: UniverseScanSummary[];
}) {
  return (
    <Panel
      title="Universe scans"
      description="Every scan for this version, newest first, exactly as the backend returned it."
    >
      {scans.length === 0 ? (
        <EmptyNote>No universe scans yet for this version — run one above.</EmptyNote>
      ) : (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              Universe scans for version {versionId}, exactly as returned by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>Date range</th>
                <th className={thClass}>Mode</th>
                <th className={thClass}>Status</th>
                <th className={thClass}>Scanned</th>
                <th className={thClass}>Succeeded</th>
                <th className={thClass}>Qualified</th>
                <th className={thClass}>Action</th>
              </tr>
            </thead>
            <tbody>
              {scans.map((s) => {
                const qualified = s.num_qualified;
                return (
                  <tr key={s.id} className={tbodyRowClass} data-testid="universe-scan-row">
                    <td className={`${tdClass} text-ink-muted`}>
                      {s.start_date} → {s.end_date}
                    </td>
                    <td className={tdClass} data-testid={`mode-${s.id}`}>
                      {modeText(s)}
                    </td>
                    <td className={tdClass}>
                      <Pill tone={statusTone(s.status)}>{s.status}</Pill>
                    </td>
                    <td className={tdClass} data-testid={`scanned-${s.id}`}>
                      {countText(s.num_symbols)}
                    </td>
                    <td className={tdClass} data-testid={`succeeded-${s.id}`}>
                      {countText(s.num_succeeded)}
                    </td>
                    <td
                      className={`${tdClass} ${
                        qualified != null && qualified > 0 ? "font-semibold text-pos" : ""
                      }`}
                      data-testid={`qualified-${s.id}`}
                    >
                      {countText(qualified)}
                    </td>
                    <td className={tdClass}>
                      <Link
                        className={btnGhost}
                        href={`/strategies/${strategyId}/universe-scans/${s.id}`}
                      >
                        View
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}

export default UniverseScanList;
