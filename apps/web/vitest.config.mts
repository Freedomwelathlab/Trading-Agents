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
