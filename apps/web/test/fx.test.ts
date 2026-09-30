import { describe, expect, it } from "vitest";
import { FX_BARS_NOTE, isFxSymbol } from "@/lib/fx";

/** Phase 103 (D123): which symbols the Markets page treats as spot FX. */
describe("isFxSymbol", () => {
  it("recognises the EURUSD.FX convention, case-insensitively", () => {
    expect(isFxSymbol("EURUSD.FX")).toBe(true);
    expect(isFxSymbol(" usdjpy.fx ")).toBe(true);
  });

  it("does not mistake equity, crypto or malformed symbols for FX", () => {
    expect(isFxSymbol("TQQQ.US")).toBe(false);
    expect(isFxSymbol("BTC-USD")).toBe(false);
    expect(isFxSymbol("EURUSD")).toBe(false);
    expect(isFxSymbol("EUR.FX")).toBe(false);
  });

  it("says the bars are mid prices with no volume", () => {
    expect(FX_BARS_NOTE).toMatch(/mid-price/);
    expect(FX_BARS_NOTE).toMatch(/no volume/);
  });
});
