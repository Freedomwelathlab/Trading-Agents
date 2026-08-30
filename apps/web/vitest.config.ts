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
      "@": path.resolve(__dirname, "."),
    },
  },
});
