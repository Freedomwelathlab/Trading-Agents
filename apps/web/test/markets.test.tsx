import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import SessionLevelsPanel, {
  toPriceLines,
  type SessionLevels,
} from "@/components/markets/SessionLevelsPanel";
import { classifyWithAbsences } from "@/lib/useKeyedFetch";

/**
 * The Markets terminal's honesty rules (Phase 74, D092).
 *
 * These assert the decisions made BEFORE anything is drawn: which levels
 * become chart lines, what an absent level looks like on screen, and how a
 * non-OK response is classified. Those are the places where a zero can
 * quietly become a price.
 */

const FULL: SessionLevels = {
  symbol: "TQQQ.US",
  bar_interval: "5m",
  session_date: "2026-09-15",
  previous_high: "70.370000",
  previous_low: "67.340000",
  previous_close: "69.270000",
  premarket_high: "69.550000",
  premarket_low: "67.780000",
  opening_range_high: "69.250000",
  opening_range_low: "68.770000",
  regular_open: "69.129000",
  vwap: "68.268642",
  vwap_upper_2sigma: "69.151543",
  vwap_lower_2sigma: "67.385741",
  bars_in_session: 192,
};

describe("toPriceLines", () => {
  it("turns every reported level into exactly one chart line", () => {
    const lines = toPriceLines(FULL);
    // Ten drawable levels; `regular_open` is context for the header rather
    // than a line, so it is deliberately not among them.
    expect(lines).toHaveLength(10);
    expect(lines.map((l) => l.price)).toContain("70.370000");
    expect(new Set(lines.map((l) => l.label)).size).toBe(lines.length);
  });

  it("omits a null level rather than drawing it at zero", () => {
    // The failure this prevents: a previous-day low at 0.00 sits below
    // every candle, so "price swept the previous low" reads as a level
    // price never reached.
    const noPremarket: SessionLevels = {
      ...FULL,
      premarket_high: null,
      premarket_low: null,
    };

    const lines = toPriceLines(noPremarket);

    expect(lines).toHaveLength(8);
    expect(lines.some((l) => l.label.includes("Premarket"))).toBe(false);
    expect(lines.every((l) => Number(l.price) > 0)).toBe(true);
  });

  it("returns nothing when there are no levels at all", () => {
    expect(toPriceLines(null)).toEqual([]);
  });

  it("gives each level a tone naming its role, not a raw colour", () => {
    const tones = new Set(toPriceLines(FULL).map((l) => l.tone));
    expect(tones).toEqual(new Set(["prior", "premarket", "opening", "vwap", "band"]));
  });
});

describe("SessionLevelsPanel", () => {
  it("renders the levels the backend reported", () => {
    render(<SessionLevelsPanel levels={FULL} unavailable={null} error={null} />);

    expect(screen.getByText("70.370000")).toBeInTheDocument();
    expect(screen.getByText("68.268642")).toBeInTheDocument();
    expect(screen.getByText(/192 bars/)).toBeInTheDocument();
  });

  it("keeps an absent level's row and marks it with a dash", () => {
    // Hiding the row instead would make "this session had no premarket
    // trading" indistinguishable from "this panel forgot about premarket".
    render(
      <SessionLevelsPanel
        levels={{ ...FULL, premarket_high: null, premarket_low: null }}
        unavailable={null}
        error={null}
      />,
    );

    expect(screen.getByText("Premarket high")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBe(2);
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("shows the backend's own sentinel when nothing is stored", () => {
    render(
      <SessionLevelsPanel
        levels={null}
        unavailable="DATA_UNAVAILABLE: no 5m bars stored for '700.HK' in the last 10 days."
        error={null}
      />,
    );

    expect(screen.getByText("DATA_UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/no 5m bars stored/)).toBeInTheDocument();
  });

  it("renders a real error distinctly from an absence", () => {
    render(
      <SessionLevelsPanel levels={null} unavailable={null} error="Could not reach the trading API." />,
    );

    expect(screen.getByText("Could not reach the trading API.")).toBeInTheDocument();
    expect(screen.queryByText("DATA_UNAVAILABLE")).not.toBeInTheDocument();
  });
});

describe("classifyWithAbsences", () => {
  const classify = classifyWithAbsences<{ ok: boolean }>([404, 503], "nothing here");

  it("treats the listed statuses as absences carrying the backend's detail", () => {
    // A 404 from the depth endpoint means the vendor answered and had no
    // priced level — an ordinary fact about a closed market, not a fault.
    expect(classify(404, { detail: "DATA_UNAVAILABLE: no priced level" })).toEqual({
      kind: "unavailable",
      detail: "DATA_UNAVAILABLE: no priced level",
    });
    expect(classify(503, null)).toEqual({ kind: "unavailable", detail: "nothing here" });
  });

  it("treats any other failure as a real error", () => {
    expect(classify(500, { detail: "boom" })).toEqual({ kind: "error", detail: "boom" });
    expect(classify(418, null)).toEqual({ kind: "error", detail: "Request failed (418)." });
  });

  it("passes a success through unchanged", () => {
    expect(classify(200, { ok: true })).toEqual({ kind: "ok", value: { ok: true } });
  });
});
