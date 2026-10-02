import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import ScanBoard, { ScoreMeter } from "@/components/bots/ScanBoard";
import type { SymbolBoard } from "@/components/bots/types";

/**
 * The bot scan board (Phase 107, D133): it shows the recommendation the
 * backend computed, the best setup's trade, and every setup's score and
 * verdict - never a confidence number the backend did not send.
 */

const BOARD: SymbolBoard = {
  symbol: "BTC-USD",
  asset_class: "crypto",
  last_close: "86048.75",
  last_ts: "2026-10-02T06:00:00Z",
  phase: "regular",
  recommendation: "BUY",
  headline: "BUY BTC-USD: sweep_mss scored 6/10. Entry 86048.75, stop 85800, 1R target 86297.5.",
  best: {
    setup: "sweep_mss", enabled: true, fired: true, direction: "long", score: 6,
    entry: "86048.75", stop: "85800", target_1r: "86297.5", risk_pct: "0.29",
    confidence_pct: "58.0", sample: 31, verdict: "qualifies",
  },
  setups: [
    {
      setup: "sweep_mss", enabled: true, fired: true, direction: "long", score: 6,
      entry: "86048.75", stop: "85800", target_1r: "86297.5", risk_pct: "0.29",
      confidence_pct: "58.0", sample: 31, verdict: "qualifies",
    },
    {
      setup: "gap_fade", enabled: true, fired: true, direction: "short", score: 3,
      entry: "86048.75", stop: "86300", target_1r: "85797.5", risk_pct: "0.29",
      confidence_pct: null, sample: null, verdict: "below_min_score",
    },
    {
      setup: "orb_failure", enabled: true, fired: false, direction: null, score: null,
      entry: null, stop: null, target_1r: null, risk_pct: null,
      confidence_pct: null, sample: null, verdict: "no_signal",
    },
  ],
  bars_used: 1728,
  calibration: "ready",
};

describe("ScanBoard", () => {
  it("shows the recommendation, the best trade and every setup's verdict", () => {
    render(<ScanBoard board={BOARD} minScore={4} />);
    expect(screen.getByText("BUY")).toBeTruthy();
    expect(screen.getByText(BOARD.headline)).toBeTruthy();
    expect(screen.getByText(/58\.0% won \+1R before the stop, 31 past signals/)).toBeTruthy();
    expect(screen.getByText("would trade")).toBeTruthy();
    expect(screen.getByText("score too low")).toBeTruthy();
    expect(screen.getByText("no signal")).toBeTruthy();
    expect(screen.getByText(/2 of 3 setups fire/)).toBeTruthy();
  });

  it("says confidence is still calculating instead of inventing one", () => {
    const board = {
      ...BOARD,
      calibration: "calculating",
      best: { ...BOARD.best!, confidence_pct: null, sample: null },
    };
    render(<ScanBoard board={board} minScore={4} />);
    expect(screen.getAllByText(/calculating/).length).toBeGreaterThan(0);
  });

  it("renders a score as a 0-10 meter", () => {
    render(<ScoreMeter score={7} min={5} />);
    const meter = screen.getByRole("meter");
    expect(meter.getAttribute("aria-valuenow")).toBe("7");
  });
});
