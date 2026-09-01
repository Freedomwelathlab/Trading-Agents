/**
 * Renders the trade-path Portfolio Manager's verdict (D029) alongside — never
 * instead of — the Risk Engine's own verdict.
 *
 * The two gates are separate and must stay visually separate: the Risk Engine
 * answers "is this one order allowed?", the Portfolio Manager answers "does the
 * resulting portfolio still fit its allocation limits?". A trade can pass the
 * first and be stopped or shrunk by the second, which is why the API can return
 * `approved: true` with `status: "rejected"`.
 *
 * Honesty rules this component enforces (D029 / docs/TRADING_SAFETY.md):
 * - A null/absent `portfolio_action` means the Portfolio Manager NEVER RAN
 *   (the Risk Engine rejected the proposal first, or no portfolio state was
 *   supplied). It is rendered as absent — nothing at all — and never as a
 *   success or an approval.
 * - An older-shaped response with no portfolio fields renders nothing. This
 *   component never invents a verdict the backend did not send.
 * - Only values the backend actually returned are displayed; missing
 *   sub-fields render as an em dash, not as a guess.
 */

/** The four `portfolio_*` fields of `TradeSubmissionResponse` (D029). All are
 * optional because an older-shaped response may omit them entirely. */
export type PortfolioVerdictFields = {
  portfolio_action?: string | null;
  portfolio_binding_constraint?: string | null;
  portfolio_detail?: string | null;
  portfolio_requested_quantity?: string | null;
};

/**
 * Colour for the Risk Engine's own result block. Keyed on the RISK verdict
 * (`approved`), not on `status`: since D029 a trade can be `status:
 * "rejected"` with `approved: true` because the Portfolio Manager stopped it,
 * and painting that amber would attribute a portfolio rejection to the Risk
 * Engine. That case gets a neutral block plus its own violet
 * `PortfolioVerdict` panel, so amber always means "the Risk Engine said no".
 */
export function riskVerdictTone(result: {
  status?: string;
  approved?: boolean;
}): string {
  if (result.approved === false)
    return "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950/70 dark:text-amber-300";
  if (result.status === "rejected")
    return "border-neutral-300 bg-neutral-100 text-neutral-800 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200";
  return "border-green-300 bg-green-50 text-green-900 dark:border-green-800 dark:bg-green-950/70 dark:text-green-300";
}

/**
 * Label/value pair inside a tinted verdict block. Deliberately inherits
 * the block's own text colour instead of using the neutral chrome token —
 * the tint is what tells the reader which gate spoke.
 */
export function VerdictTerm({ children }: { children: React.ReactNode }) {
  return (
    <dt className="font-mono text-[11px] uppercase tracking-wide opacity-70">
      {children}
    </dt>
  );
}

export function VerdictValue({
  children,
  testId,
}: {
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <dd data-testid={testId} className="tnum truncate font-mono text-[13px]">
      {children}
    </dd>
  );
}

type Props = PortfolioVerdictFields & {
  /** The quantity that actually reached the broker, for the resize comparison. */
  fill_quantity?: string | null;
};

export default function PortfolioVerdict({
  portfolio_action,
  portfolio_binding_constraint,
  portfolio_detail,
  portfolio_requested_quantity,
  fill_quantity,
}: Props) {
  // Never ran, or an older-shaped response. Absent, not approved.
  if (!portfolio_action) return null;
  // An approve adds no information the existing result block doesn't already
  // convey, so it deliberately adds no UI noise either.
  if (portfolio_action === "approve") return null;

  const isModify = portfolio_action === "modify";
  const isReject = portfolio_action === "reject";

  // Deliberately NOT the amber a risk rejection uses: a portfolio rejection is
  // a different gate with a different meaning, and conflating the two colours
  // would make an easily-misread combination worse.
  const tone = isReject
    ? "border-violet-400 bg-violet-50 text-violet-900 dark:border-violet-700 dark:bg-violet-950/70 dark:text-violet-200"
    : isModify
      ? "border-blue-400 bg-blue-50 text-blue-900 dark:border-blue-700 dark:bg-blue-950/70 dark:text-blue-200"
      : "border-neutral-400 bg-neutral-50 text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200";

  const heading = isReject
    ? "⛔ Rejected by the Portfolio Manager (not the Risk Engine)"
    : isModify
      ? "⇅ Resized by the Portfolio Manager"
      : `Portfolio Manager action: ${portfolio_action}`;

  return (
    <div
      data-testid="portfolio-verdict"
      data-portfolio-action={portfolio_action}
      className={`rounded-md border-l-4 border border-l-current px-3 py-3 text-sm ${tone}`}
    >
      <p className="text-[11px] font-semibold uppercase tracking-[0.09em] opacity-70">
        Portfolio Manager
      </p>
      <p className="mt-1 font-semibold">{heading}</p>

      {isReject && (
        <p className="mt-1.5 leading-relaxed">
          The deterministic Risk Engine did not stop this trade — the
          portfolio-level allocation gate did. No order reached the broker.
        </p>
      )}

      {isModify && (
        <p className="mt-1.5 leading-relaxed">
          The Risk Engine approved this trade, then the Portfolio Manager shrank
          it to fit a portfolio-level limit. The reduced quantity was re-checked
          by the Risk Engine before it reached the broker.
        </p>
      )}

      <dl className="mt-3 grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1.5 sm:grid-cols-[auto_1fr_auto_1fr]">
        <VerdictTerm>portfolio_action</VerdictTerm>
        <VerdictValue>{portfolio_action}</VerdictValue>
        <VerdictTerm>binding constraint</VerdictTerm>
        <VerdictValue>{portfolio_binding_constraint ?? "—"}</VerdictValue>
        {isModify && (
          <>
            <VerdictTerm>requested quantity</VerdictTerm>
            <VerdictValue testId="portfolio-requested-quantity">
              {portfolio_requested_quantity ?? "—"}
            </VerdictValue>
            <VerdictTerm>filled quantity</VerdictTerm>
            <VerdictValue testId="portfolio-filled-quantity">
              {fill_quantity ?? "—"}
            </VerdictValue>
          </>
        )}
      </dl>

      {portfolio_detail && <p className="mt-2 leading-relaxed">{portfolio_detail}</p>}
    </div>
  );
}
