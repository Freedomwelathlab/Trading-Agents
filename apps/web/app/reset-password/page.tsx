"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import AuthCard from "@/components/auth/AuthCard";
import { Alert, Field, btnPrimary, inputClass } from "@/components/ui/primitives";

/**
 * `/reset-password?token=...` (Phase 46 / docs/DECISIONS.md D062).
 *
 * Two honesty rules govern everything below.
 *
 * 1. The backend answers every failure with one sentinel,
 *    `INVALID_OR_EXPIRED_TOKEN`, and this page renders that sentinel
 *    verbatim. It does NOT guess which of "expired", "already used" or
 *    "never existed" happened — the backend withholds that distinction
 *    deliberately (knowing it would confirm that a real reset was
 *    requested for a real account), so inventing a friendlier reason here
 *    would be a fabrication and a small security regression at once.
 *
 * 2. The confirm-password check is a CLIENT-SIDE TYPO GUARD and nothing
 *    more. It compares two boxes the same person just typed; it is not a
 *    validation the backend performs or trusts, and the real password
 *    floor (8 characters) is enforced server-side by the request schema.
 *    A mismatch never reaches the network.
 *
 * Success does not sign the user in. They are sent to `/login` to use the
 * password they just chose, which proves the new credential works.
 */
export default function ResetPasswordPage() {
  return (
    <Suspense fallback={null}>
      <ResetPasswordForm />
    </Suspense>
  );
}

function ResetPasswordForm() {
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [succeeded, setSucceeded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (password !== confirmation) {
      setError("The two passwords do not match.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetch("/api/auth/password-reset/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: password }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        // Whatever the backend said, including the raw
        // INVALID_OR_EXPIRED_TOKEN sentinel and the 422 the schema
        // returns for a too-short password.
        setError(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setSucceeded(true);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setSubmitting(false);
    }
  }

  if (succeeded) {
    return (
      <AuthCard title="Password updated" subtitle="This reset link is now spent.">
        <Alert tone="info" role="status" testId="reset-success">
          Your password has been changed. Sign in with the new one.
        </Alert>
        <Link href="/login" className={`${btnPrimary} w-full`}>
          Go to sign in
        </Link>
      </AuthCard>
    );
  }

  // No token in the URL at all is a broken link, and saying so is not a
  // guess — it is a fact about this page's own address, not a claim about
  // the state of any token on the server.
  if (!token) {
    return (
      <AuthCard title="Reset your password" subtitle="This link is incomplete.">
        <Alert tone="error">
          This page needs a reset token in its address (…/reset-password?token=…).
          Request a new link and open it directly from the message you were sent.
        </Alert>
        <Link
          href="/forgot-password"
          className="text-center text-xs font-semibold text-accent hover:underline"
        >
          Request a new link
        </Link>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Choose a new password"
      subtitle="This link can be used once."
      footer={
        <>
          Using this link signs nothing in — you&apos;ll sign in with the new
          password afterwards. Any other outstanding reset link for this
          account stops working once this one is used.
        </>
      }
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <Field label="New password" hint="At least 8 characters.">
          <input
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            className={inputClass}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>

        <Field label="Confirm new password">
          <input
            type="password"
            required
            autoComplete="new-password"
            className={inputClass}
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
          />
        </Field>

        {error && <Alert tone="error">{error}</Alert>}

        <button type="submit" disabled={submitting} className={`${btnPrimary} w-full`}>
          {submitting ? "Updating..." : "Set new password"}
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
