import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    // The Playwright e2e suite (`npm run test:e2e`) lives in ./e2e and needs a
    // real backend, a real database and a real browser. Vitest must never pick
    // those files up: they are a deliberately separate suite (D044), and its
    // default `**/*.spec.ts` glob would otherwise match them.
    exclude: ["node_modules/**", "e2e/**", ".next/**"],
    // Raised from Vitest's 5s default (Phase 74, D092).
    //
    // The Recharts suites render a full SVG chart in jsdom, which is slow
    // and - more to the point - slows down superlinearly under parallel
    // worker contention. Measured on this machine: `DrawdownChart`'s
    // area-render case takes 476ms run on its own and 5,437ms inside the
    // full suite, and `EquityCurveChart`'s legend case 518ms against
    // 5,716ms. Both were failing the 5s default while being perfectly
    // correct.
    //
    // This is deliberately NOT a blanket "tests are flaky, give them
    // longer": a 10x spread between isolated and contended runs is a
    // resource property, and the isolated timings show there is no hang to
    // hide. A test that genuinely hangs still fails here, just 15s later.
    testTimeout: 15_000,
  },
  resolve: {
    alias: {
      // `import.meta.dirname`, not `__dirname`: this file is ESM (`.mts`),
      // and Vite's native config loader - already the default in Vitest 4's
      // Vite, mandatory in a future major - does not inject the CommonJS
      // globals. See docs/DECISIONS.md D050.
      "@": path.resolve(import.meta.dirname, "."),
    },
  },
});
