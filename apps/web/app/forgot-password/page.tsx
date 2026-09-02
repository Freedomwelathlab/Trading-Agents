"use client";

import Link from "next/link";
import { useState } from "react";
import AuthCard from "@/components/auth/AuthCard";
import { Alert, Field, btnPrimary, inputClass } from "@/components/ui/primitives";

/**
 * `/forgot-password` (Phase 46 / docs/DECISIONS.md D062).
 *
 * The one rule this page exists to honour: the backend answers 200 with a
 * single fixed message whether or not the address is registered, and this
 * page renders that message verbatim. It never says "check your inbox",
 * never says "we couldn't find that account", and never varies by
 * anything it could infer — the anti-enumeration property is only as
 * strong as its weakest renderer, and a client that split the same 200
 * into two different screens would undo the whole design.
 *
 * The form is deliberately left on screen after a successful submit
 * rather than being replaced by a confirmation panel: a user whose link
 * never arrives (a deployment with no email provider configured, which is
 * the committed default) needs to be able to read the message and act on
 * it, not stare at a dead end.
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [acknowledgement, setAcknowledgement] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setAcknowledgement(null);
    try {
      const res = await fetch("/api/auth/password-reset/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        // A real failure — a 429 from the throttle, a 503 from a dead
        // backend — is shown as itself. This branch is never reached by
        // "no such email", which is a 200.
        setError(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      // The backend's own sentence, not a rewrite of it.
      setAcknowledgement(
        data?.detail ??
          "Your request was accepted. If no email arrives, contact an administrator.",
      );
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthCard
      title="Reset your password"
      subtitle="We'll issue a single-use link for the account, if it exists."
      footer={
        <>
          Reset links expire and can be used once. If this deployment has no
          email provider configured, an administrator can issue the link for
          you instead.
        </>
      }
    >
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

        {acknowledgement && (
          <Alert tone="info" role="status" testId="reset-acknowledgement">
            {acknowledgement}
          </Alert>
        )}
        {error && <Alert tone="error">{error}</Alert>}

        <button type="submit" disabled={submitting} className={`${btnPrimary} w-full`}>
          {submitting ? "Sending..." : "Send reset link"}
        </button>
      </form>

      <Link
        href="/login"
        className="text-center text-xs font-semibold text-accent hover:underline"
      >
        Back to sign in
      </Link>
    </AuthCard>
  );
}
