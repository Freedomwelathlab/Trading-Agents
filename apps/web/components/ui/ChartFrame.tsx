import type { ReactNode } from "react";

/**
 * The shared SVG frame both equity charts draw into (Phase 45 / D060).
 *
 * Still hand-rolled, for the reason both charts already documented: a
 * single-series line does not justify a new charting dependency. This
 * component only adds *chrome* — a plot background, three low-contrast
 * gridlines, and the min/max value labels the previous version left the
 * reader to infer. It draws no data of its own: the `linePoints` and
 * `areaPoints` strings, and the marker `children`, are computed by each
 * chart's own `buildPoints`/`buildBacktestPoints` from values the backend
 * actually returned. Nothing is interpolated, smoothed, or extrapolated
 * here.
 *
 * The geometry constants must stay identical to the ones each chart uses
 * to project its points, so they live here and are imported there.
 */

export const WIDTH = 640;
export const HEIGHT = 200;
export const PAD_LEFT = 8;
export const PAD_RIGHT = 8;
export const PAD_TOP = 12;
export const PAD_BOTTOM = 12;

export function ChartFrame({
  ariaLabel,
  min,
  max,
  linePoints,
  areaPoints,
  testId,
  children,
}: {
  ariaLabel: string;
  /** The real minimum plotted value, labelled on the axis. */
  min: number;
  /** The real maximum plotted value, labelled on the axis. */
  max: number;
  linePoints: string;
  areaPoints: string;
  /** Kept on the `<polyline>` so existing tests still identify the series. */
  testId: string;
  /** Point markers, each carrying its own `<title>` tooltip. */
  children: ReactNode;
}) {
  const top = PAD_TOP;
  const bottom = HEIGHT - PAD_BOTTOM;
  const mid = (top + bottom) / 2;
  const gradientId = `${testId}-fill`;

  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      className="w-full"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--pos)" stopOpacity="0.28" />
          <stop offset="100%" stopColor="var(--pos)" stopOpacity="0.02" />
        </linearGradient>
      </defs>

      {/* Gridlines: low-contrast so they never compete with the series. */}
      {[top, mid, bottom].map((y) => (
        <line
          key={y}
          x1={PAD_LEFT}
          x2={WIDTH - PAD_RIGHT}
          y1={y}
          y2={y}
          stroke="var(--grid)"
          strokeWidth={1}
          strokeDasharray={y === bottom ? undefined : "3 4"}
        />
      ))}

      {areaPoints && <polygon points={areaPoints} fill={`url(#${gradientId})`} />}

      <polyline
        data-testid={testId}
        points={linePoints}
        fill="none"
        stroke="var(--pos)"
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />

      {children}

      {/* Axis value labels. Rendered aria-hidden because the exact figures
          are already in this chart's own data table below it. */}
      <text
        x={WIDTH - PAD_RIGHT}
        y={top - 2}
        textAnchor="end"
        aria-hidden="true"
        fill="var(--ink-faint)"
        style={{ fontSize: 9, fontFamily: "var(--font-mono-code), monospace" }}
      >
        {max}
      </text>
      <text
        x={WIDTH - PAD_RIGHT}
        y={bottom + 9}
        textAnchor="end"
        aria-hidden="true"
        fill="var(--ink-faint)"
        style={{ fontSize: 9, fontFamily: "var(--font-mono-code), monospace" }}
      >
        {min}
      </text>
    </svg>
  );
}
