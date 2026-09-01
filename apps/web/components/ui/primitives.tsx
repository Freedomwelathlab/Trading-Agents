import type { ReactNode } from "react";

/**
 * Shared presentational primitives for the Phase 45 dashboard redesign
 * (D060).
 *
 * These are style-only. None of them fetch, none of them decide what to
 * render — every panel, alert and value on the dashboard is still driven
 * entirely by what its owning component got back from the backend. They
 * exist so the eight panels stop each hand-rolling their own borders,
 * paddings and button colours.
 *
 * Two constraints are deliberate and load-bearing:
 *
 * 1. `Field` keeps the label text as the *only* text inside its
 *    `<label>` element, and puts any hint in a sibling paragraph. The
 *    accessible name of an input therefore stays exactly its label, which
 *    is what both the component tests and a screen-reader user rely on.
 * 2. Nothing here is built on a component library. The project's
 *    ask-before-adding-a-tool rule applies to UI dependencies too, so
 *    this is plain Tailwind against the tokens in `globals.css`.
 */

/* ------------------------------------------------------------------ */
/* Control surfaces                                                     */
/* ------------------------------------------------------------------ */

export const inputClass =
  "w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink " +
  "placeholder:text-ink-faint outline-none transition-colors duration-150 " +
  "hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 " +
  "disabled:cursor-not-allowed disabled:opacity-50";

export const monoInputClass = `${inputClass} font-mono text-[13px] tnum`;

export const labelClass =
  "flex flex-col gap-1.5 text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-muted";

export const hintClass = "text-xs leading-relaxed text-ink-faint";

const buttonBase =
  "inline-flex cursor-pointer items-center justify-center gap-2 rounded-md " +
  "text-sm font-semibold transition-all duration-150 " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:cursor-not-allowed disabled:opacity-50";

export const btnPrimary =
  `${buttonBase} bg-accent px-4 py-2 text-accent-ink shadow-sm ` +
  "hover:brightness-110 active:brightness-95";

export const btnSecondary =
  `${buttonBase} border border-line-strong bg-raised px-3.5 py-2 text-ink ` +
  "hover:border-accent hover:text-accent";

export const btnGhost =
  `${buttonBase} border border-line px-2.5 py-1 text-xs text-ink-muted ` +
  "hover:border-accent hover:text-accent";

/* ------------------------------------------------------------------ */
/* Panel                                                                */
/* ------------------------------------------------------------------ */

export function Panel({
  title,
  description,
  actions,
  children,
  className = "",
  headingId,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  headingId?: string;
}) {
  return (
    <section
      className={
        "flex flex-col overflow-hidden rounded-lg border border-line bg-surface " +
        "shadow-[var(--shadow-panel)] " +
        className
      }
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line bg-raised/60 px-4 py-3">
        <div className="min-w-0">
          <h2
            id={headingId}
            className="flex items-center gap-2 text-sm font-semibold tracking-tight text-ink"
          >
            <span
              aria-hidden="true"
              className="h-3.5 w-[3px] shrink-0 rounded-full bg-accent"
            />
            {title}
          </h2>
          {description && (
            <p className={`mt-1.5 max-w-prose ${hintClass}`}>{description}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </header>
      <div className="flex flex-1 flex-col gap-4 p-4">{children}</div>
    </section>
  );
}

/** A labelled band that groups several panels under one heading. */
export function SectionHeading({
  children,
  note,
}: {
  children: ReactNode;
  note?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 pt-2">
      <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-faint">
        {children}
      </h2>
      <span className="h-px flex-1 bg-line" aria-hidden="true" />
      {note && <span className="text-xs text-ink-faint">{note}</span>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Form field                                                           */
/* ------------------------------------------------------------------ */

/**
 * `label` is rendered as the sole text child of the `<label>` element, so
 * the wrapped control's accessible name is exactly `label`. `hint` is a
 * sibling and never joins the accessible name.
 */
export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-col gap-1.5 ${className}`}>
      <label className={labelClass}>
        {label}
        {children}
      </label>
      {hint && <p className={hintClass}>{hint}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Alerts                                                               */
/* ------------------------------------------------------------------ */

const alertTones = {
  /** A real backend failure: a sentinel string, a 4xx/5xx, a dead socket. */
  error:
    "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300",
  /** Something the user should notice but that is not a failure. */
  warn: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300",
  /** Neutral explanatory copy, e.g. a real empty result. */
  info: "border-line bg-well text-ink-muted",
} as const;

export type AlertTone = keyof typeof alertTones;

export function Alert({
  tone = "error",
  role = "alert",
  children,
  className = "",
  testId,
}: {
  tone?: AlertTone;
  role?: "alert" | "status";
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <p
      role={role}
      data-testid={testId}
      className={
        "rounded-md border px-3 py-2 text-sm leading-relaxed " +
        `${alertTones[tone]} ${className}`
      }
    >
      {children}
    </p>
  );
}

/** An empty/neutral state that is a real backend answer, not a failure. */
export function EmptyNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-line bg-well px-3 py-3 text-sm text-ink-muted">
      {children}
    </p>
  );
}

/* ------------------------------------------------------------------ */
/* Data display                                                         */
/* ------------------------------------------------------------------ */

/**
 * A key/value readout. Keeps the `<dl>/<dt>/<dd>` semantics the previous
 * panels used — the keys are the backend's own field names on purpose, so
 * what is on screen stays traceable to what the API returned.
 */
export function KeyValue({
  children,
  columns = 2,
  className = "",
  testId,
}: {
  children: ReactNode;
  columns?: 1 | 2 | 3;
  className?: string;
  testId?: string;
}) {
  const cols =
    columns === 3
      ? "sm:grid-cols-[auto_1fr_auto_1fr_auto_1fr]"
      : columns === 1
        ? "grid-cols-[auto_1fr]"
        : "grid-cols-[auto_1fr] sm:grid-cols-[auto_1fr_auto_1fr]";
  return (
    <dl
      data-testid={testId}
      className={`grid ${cols} items-baseline gap-x-4 gap-y-1.5 text-sm ${className}`}
    >
      {children}
    </dl>
  );
}

export function Term({ children }: { children: ReactNode }) {
  return (
    <dt className="font-mono text-[11px] uppercase tracking-wide text-ink-faint">
      {children}
    </dt>
  );
}

export function Value({
  children,
  testId,
  tone,
}: {
  children: ReactNode;
  testId?: string;
  tone?: "pos" | "neg";
}) {
  const color = tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "text-ink";
  return (
    <dd
      data-testid={testId}
      className={`tnum truncate font-mono text-[13px] ${color}`}
    >
      {children}
    </dd>
  );
}

/** A headline figure. Used for the few numbers that lead a panel. */
export function Stat({
  label,
  value,
  hint,
  tone,
  testId,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "pos" | "neg" | "accent";
  testId?: string;
}) {
  const color =
    tone === "pos"
      ? "text-pos"
      : tone === "neg"
        ? "text-neg"
        : tone === "accent"
          ? "text-accent"
          : "text-ink";
  return (
    <div className="rounded-md border border-line bg-well px-3 py-2.5">
      <p className="text-[10px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
        {label}
      </p>
      <p
        data-testid={testId}
        className={`tnum mt-1 truncate font-mono text-lg leading-tight ${color}`}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-[11px] text-ink-faint">{hint}</p>}
    </div>
  );
}

/** Wraps a wide table so the page body never scrolls sideways. */
export function TableScroll({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`-mx-1 overflow-x-auto rounded-md border border-line px-1 ${className}`}
    >
      {children}
    </div>
  );
}

export const tableClass = "w-full min-w-max text-left text-xs";
export const theadRowClass =
  "border-b border-line text-[10px] uppercase tracking-[0.09em] text-ink-faint";
export const thClass = "whitespace-nowrap px-2.5 py-2 font-semibold";
export const tdClass = "tnum whitespace-nowrap px-2.5 py-1.5 font-mono text-ink";
export const tbodyRowClass =
  "border-b border-line/60 last:border-0 transition-colors hover:bg-well";

/** A small status pill. `tone` is always derived from real backend data. */
export function Pill({
  children,
  tone = "neutral",
  testId,
}: {
  children: ReactNode;
  tone?: "neutral" | "pos" | "neg" | "warn";
  testId?: string;
}) {
  const tones = {
    neutral: "border-line-strong bg-well text-ink-muted",
    pos: "border-emerald-500/40 bg-emerald-500/10 text-pos",
    neg: "border-red-500/40 bg-red-500/10 text-neg",
    warn: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  } as const;
  return (
    <span
      data-testid={testId}
      className={
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 " +
        `font-mono text-[11px] font-medium ${tones[tone]}`
      }
    >
      {children}
    </span>
  );
}
