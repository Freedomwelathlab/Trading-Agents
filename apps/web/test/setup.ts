import "@testing-library/jest-dom/vitest";

/**
 * jsdom implements neither `matchMedia` nor `ResizeObserver`, and both are
 * real browser APIs that any canvas-rendering or responsive component will
 * reach for. `lightweight-charts` (Phase 70, D088) reaches for both on
 * construction - `matchMedia` to watch device-pixel-ratio changes, and
 * `ResizeObserver` for `autoSize` - and without them the chart raises
 * unhandled rejections that surface as errors on unrelated test files.
 *
 * These are STUBS OF MISSING BROWSER APIS, not mocks of our own code. They
 * assert nothing and let nothing pass that would otherwise fail: the
 * behaviour they stand in for is device-pixel-ratio tracking and element
 * resizing, neither of which exists in a headless DOM to be tested. The
 * component's own decisions - which bars are drawable, where a marker goes
 * - are tested directly against its exported transforms, precisely because
 * the canvas itself is not observable here.
 */
if (typeof window !== "undefined") {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }

  if (!window.ResizeObserver) {
    window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof window.ResizeObserver;
  }
}
