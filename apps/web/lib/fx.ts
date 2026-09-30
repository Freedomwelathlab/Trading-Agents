/**
 * Spot FX on the Markets page (Phase 103, D123).
 *
 * `EURUSD.FX` - six letters, base then quote, and a `.FX` suffix - is the
 * backend's FX convention (`apps/api/app/marketdata/fx.py`). The bars it
 * stores for a pair are quotes, not trades: mid prices with no volume.
 */
export function isFxSymbol(symbol: string): boolean {
  return /^[A-Z]{6}\.FX$/.test(symbol.trim().toUpperCase());
}

/** Shown beside an FX chart, so an empty volume pane or an absent VWAP
 *  does not read as a quiet market. */
export const FX_BARS_NOTE =
  "Spot FX · mid-price bars (bid/ask average), no volume. Sessions run 17:00–17:00 New York; " +
  "volume-based setups and VWAP do not apply.";
