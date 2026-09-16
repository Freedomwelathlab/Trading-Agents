import { beforeAll, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  PriceChart,
  toCandles,
  toMarkers,
  type PriceBar,
  type SignalMarker,
} from "@/components/PriceChart";

/**
 * The candlestick chart's honesty rules (Phase 70, D088).
 *
 * `lightweight-charts` draws to a canvas that jsdom does not implement, so
 * nothing here asserts that a candle was painted — that is not knowable in
 * this environment and a test claiming it would be testing a mock. What IS
 * asserted is every decision the component makes BEFORE handing anything to
 * the library: which bars are drawable, which signals land on which bar,
 * and what the user is told when neither is possible. Those are the rules
 * that can silently start fabricating market structure; the rendering
 * itself is TradingView's.
 */

/**
 * jsdom ships no 2D canvas context, so `lightweight-charts` cannot
 * construct or tear down without one - `getContext` raises "Not
 * implemented" unless the optional native `canvas` package is installed,
 * which is a heavyweight build dependency this project has no other use
 * for.
 *
 * **Scoped to this file, deliberately, and NOT put in `test/setup.ts`.** It
 * was there first, and it broke five unrelated suites: Recharts measures
 * text through `ctx.measureText(...).width`, so a globally stubbed context
 * that answered every method with `undefined` made every Recharts-based
 * chart test time out. An environment stub that changes how OTHER
 * components behave under test is not a stub, it is a mutation of the test
 * environment - so it lives beside the one component that needs it.
 *
 * Nothing is asserted through it. No test here inspects pixels; the
 * component's real decisions are asserted against its exported transforms
 * precisely because the canvas is not observable in jsdom.
 */
beforeAll(() => {
  const noopContext = new Proxy(
    {},
    {
      get: (_t, prop) => {
        // Enough shape for a measurement call to destructure a width,
        // which is the one place a caller reads rather than draws.
        if (prop === "measureText") return () => ({ width: 0 });
        if (prop === "canvas") return undefined;
        return typeof prop === "string" && /^[a-z]/.test(prop) ? () => undefined : 0;
      },
      set: () => true,
    },
  );
  HTMLCanvasElement.prototype.getContext = (() =>
    noopContext) as unknown as HTMLCanvasElement["getContext"];
});

function bar(overrides: Partial<PriceBar> = {}): PriceBar {
  return {
    ts: "2026-03-02T21:00:00Z",
    open: "100",
    high: "110",
    low: "95",
    close: "105",
    volume: 1000,
    source: "longbridge",
    ...overrides,
  };
}

describe("toCandles", () => {
  it("converts a complete bar to a candle on its own timestamp", () => {
    const { candles, skipped } = toCandles([bar()]);
    expect(skipped).toBe(0);
    expect(candles).toEqual([
      {
        // 2026-03-02T21:00:00Z in seconds — taken from the bar's own vendor
        // timestamp, never from an assumed session close.
        time: Math.floor(Date.parse("2026-03-02T21:00:00Z") / 1000),
        open: 100,
        high: 110,
        low: 95,
        close: 105,
      },
    ]);
  });

  it("skips a close-only bar rather than inventing a body from the close", () => {
    // `market_data_bars.open/high/low` are nullable. Filling them from the
    // close would draw a candle — a doji — that asserts the market opened
    // and closed at the same price and never moved. Nothing knows that.
    const { candles, skipped } = toCandles([
      bar(),
      bar({ ts: "2026-03-03T21:00:00Z", open: null, high: null, low: null }),
    ]);
    expect(candles).toHaveLength(1);
    expect(skipped).toBe(1);
  });

  it("skips a bar missing only its high, not just a fully empty one", () => {
    // A partial record is exactly as undrawable as an empty one: a candle
    // needs all four prices, and three plus a guess is still a guess.
    const { candles, skipped } = toCandles([bar({ high: null })]);
    expect(candles).toHaveLength(0);
    expect(skipped).toBe(1);
  });

  it("skips a bar whose timestamp cannot be parsed", () => {
    const { candles, skipped } = toCandles([bar({ ts: "not-a-timestamp" })]);
    expect(candles).toHaveLength(0);
    expect(skipped).toBe(1);
  });

  it("preserves the order it was given", () => {
    // The backend returns bars oldest-first and lightweight-charts requires
    // that ordering; re-sorting here would mask a backend that stopped.
    const { candles } = toCandles([
      bar({ ts: "2026-03-02T21:00:00Z", close: "1" }),
      bar({ ts: "2026-03-03T21:00:00Z", close: "2" }),
      bar({ ts: "2026-03-04T21:00:00Z", close: "3" }),
    ]);
    expect(candles.map((c) => c.close)).toEqual([1, 2, 3]);
  });
});

describe("toMarkers", () => {
  const times = [
    Math.floor(Date.parse("2026-03-02T21:00:00Z") / 1000),
    Math.floor(Date.parse("2026-03-03T21:00:00Z") / 1000),
  ] as Parameters<typeof toMarkers>[1];

  const buy: SignalMarker = { date: "2026-03-02", side: "buy", price: "100.15" };
  const sell: SignalMarker = { date: "2026-03-03", side: "sell", price: "99.85" };

  it("places a buy below its bar and a sell above, pointing the right way", () => {
    const markers = toMarkers([buy, sell], times);
    expect(markers).toHaveLength(2);
    expect(markers[0]).toMatchObject({ position: "belowBar", shape: "arrowUp" });
    expect(markers[1]).toMatchObject({ position: "aboveBar", shape: "arrowDown" });
  });

  it("labels each marker with its side and executed price", () => {
    // The executed price, which from Phase 70 on is NET of costs — so the
    // label agrees with the equity curve rather than with the raw close the
    // marker sits on.
    const markers = toMarkers([buy, sell], times);
    expect(markers[0].text).toBe("BUY @ 100.15");
    expect(markers[1].text).toBe("SELL @ 99.85");
  });

  it("drops a signal whose bar is not in the loaded window", () => {
    // Never snapped to the nearest bar. A marker on a bar the trade did not
    // happen on is a plausible-looking lie about when the strategy acted,
    // which is worse than a marker that is simply absent.
    const markers = toMarkers(
      [{ date: "2019-01-01", side: "buy", price: "1" }, buy],
      times,
    );
    expect(markers).toHaveLength(1);
    expect(markers[0].text).toBe("BUY @ 100.15");
  });

  it("returns markers in ascending time order whatever order they arrive in", () => {
    // The trade ledger is sorted by entry_date, so mapping each round trip
    // to two legs interleaves exits out of order. The library misrenders an
    // unsorted marker list without complaining.
    const markers = toMarkers([sell, buy], times);
    expect(markers.map((m) => m.time)).toEqual([...times].sort((a, b) => a - b));
  });

  it("returns nothing when there are no signals", () => {
    expect(toMarkers([], times)).toEqual([]);
  });
});

describe("PriceChart fallbacks", () => {
  it("says the bars are not ingested rather than drawing an empty frame", () => {
    render(<PriceChart bars={[]} />);
    expect(screen.getByText(/No bars are ingested/i)).toBeInTheDocument();
    expect(screen.getByText(/backfill/i)).toBeInTheDocument();
  });

  it("explains a window that is entirely close-only", () => {
    render(
      <PriceChart bars={[bar({ open: null, high: null, low: null })]} />,
    );
    expect(screen.getByText(/close-only/i)).toBeInTheDocument();
    // The distinction that matters: this window HAS data, it just cannot be
    // drawn as candles. Telling the user to backfill would be wrong advice.
    expect(screen.queryByText(/No bars are ingested/i)).not.toBeInTheDocument();
  });

  it("discloses partially skipped bars instead of quietly dropping them", () => {
    render(
      <PriceChart
        bars={[bar(), bar({ ts: "2026-03-03T21:00:00Z", high: null })]}
      />,
    );
    expect(screen.getByText(/1 of 2 bar\(s\) omitted/i)).toBeInTheDocument();
  });
});
