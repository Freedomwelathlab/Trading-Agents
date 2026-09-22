"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { EXPIRED_REASON } from "@/lib/session";
import {
  Alert,
  Field,
  btnPrimary,
  hintClass,
  inputClass,
} from "@/components/ui/primitives";

/**
 * Explains why the user is back on this page when they were bounced here
 * by a real 401 from a route handler (D031) — an expired session, or one
 * whose user was deactivated — instead of leaving them at a login form
 * with no context. Rendered inside a Suspense boundary because
 * `useSearchParams` requires one.
 */
function SessionEndedNotice() {
  const reason = useSearchParams().get("reason");
  if (reason !== EXPIRED_REASON) return null;
  return (
    <Alert tone="warn" role="status">
      Your session has ended — it either expired or your account was
      deactivated. Sign in again to continue.
    </Alert>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}

/**
 * Phase 45 / D060 restyle. The credential handling, the redirect and the
 * error rendering are unchanged: whatever `detail` the backend sends —
 * including the real lockout message — is shown verbatim, and a failure
 * to reach the service says exactly that rather than guessing at a cause.
 */
function LoginForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(data?.detail ?? `Login failed (HTTP ${res.status})`);
        return;
      }
      router.push("/dashboard");
      router.refresh();
    } catch {
      setError("Could not reach the login service.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden px-4 py-10">
      {/* Ambient ground. Decorative only, and behind everything — it never
          sits between the reader and a value. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10"
        style={{
          backgroundImage:
            "radial-gradient(60rem 32rem at 50% -12%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 70%)",
        }}
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10 opacity-[0.5]"
        style={{
          backgroundImage:
            "linear-gradient(var(--grid) 1px, transparent 1px), linear-gradient(90deg, var(--grid) 1px, transparent 1px)",
          backgroundSize: "56px 56px",
          maskImage: "radial-gradient(40rem 26rem at 50% 30%, black, transparent 75%)",
          WebkitMaskImage:
            "radial-gradient(40rem 26rem at 50% 30%, black, transparent 75%)",
        }}
      />

      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <svg viewBox="0 0 28 28" className="h-11 w-11" role="img" aria-label="Trading OS">
            <rect
              x="1"
              y="1"
              width="26"
              height="26"
              rx="7"
              className="fill-accent/12 stroke-accent/50"
              strokeWidth="1"
            />
            <path
              d="M8 19.5 12.5 14l3.5 3 4-7.5"
              fill="none"
              className="stroke-accent"
              strokeWidth="1.9"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <circle cx="20" cy="9.5" r="1.9" className="fill-accent" />
          </svg>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-ink">Trading OS</h1>
            <p className={`mt-1 ${hintClass}`}>Sign in to continue.</p>
          </div>
        </div>

        <div className="flex flex-col gap-4 rounded-xl border border-line bg-surface p-6 shadow-[var(--shadow-panel)]">
          <SessionEndedNotice />

          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <Field label="Email">
              <input
                type="email"
                required
                autoComplete="username"
                className={inputClass}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>

            <Field label="Password">
              <input
                type="password"
                required
                autoComplete="current-password"
                className={inputClass}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>

            {error && <Alert tone="error">{error}</Alert>}

            <button type="submit" disabled={submitting} className={`${btnPrimary} w-full`}>
              {submitting ? "Signing in..." : "Sign in"}
            </button>
          </form>

          {/* Phase 46 / D063. Always shown, never conditional on a failed
              attempt: a user who cannot log in should not have to fail
              first to discover that a reset exists. */}
          <Link
            href="/forgot-password"
            className="text-center text-xs font-semibold text-accent hover:underline"
          >
            Forgot password?
          </Link>
        </div>

        <p className="mt-5 text-center text-xs leading-relaxed text-ink-faint">
          Sessions are held in an httpOnly cookie and end after 15 minutes without
          input (or 12 hours in all). Trading mode and the live-trading switch are
          enforced by the backend, never by this page.
        </p>
      </div>
    </main>
  );
}
