import { execSync } from "node:child_process";
import path from "node:path";

/**
 * Re-seeds the real database before the suite runs (docs/DECISIONS.md D045).
 *
 * Several specs submit real trades that permanently change real broker rows —
 * the Portfolio Manager MODIFY spec in particular only produces a MODIFY while
 * the concentrated broker still holds exactly its seeded position. Re-seeding
 * once per run is what makes those specs repeatable without weakening them
 * into "assert whatever came back".
 *
 * Set `E2E_SKIP_SEED=1` to skip (e.g. when you just ran the seed by hand),
 * or `E2E_SEED_COMMAND` to point at a specific interpreter/venv.
 */
export default function globalSetup(): void {
  if (process.env.E2E_SKIP_SEED === "1") {
    console.log("[e2e] E2E_SKIP_SEED=1 — not re-seeding the database.");
    return;
  }
  const command = process.env.E2E_SEED_COMMAND ?? "python scripts/seed_e2e.py";
  const repoRoot = path.resolve(__dirname, "..", "..", "..");
  console.log(`[e2e] seeding: ${command} (cwd=${repoRoot})`);
  execSync(command, { cwd: repoRoot, stdio: "inherit" });
}
