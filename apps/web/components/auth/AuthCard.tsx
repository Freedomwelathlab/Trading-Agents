import type { ReactNode } from "react";

/**
 * The unauthenticated page shell (Phase 46 / D062).
 *
 * Extracted from `app/login/page.tsx`'s Phase 45 layout so `/login`,
 * `/forgot-password` and `/reset-password` are visibly one flow rather
 * than three pages that happen to share a colour palette. Identical
 * markup, identical tokens, identical ambient background — this is a
 * move, not a redesign.
 *
 * Deliberately NOT `components/shell/AppShell`. That shell renders
 * `SessionStatus` and `LogoutButton`, which poll `GET /auth/session` and
 * bounce a caller to `/login` on the 401 they are guaranteed to get here:
 * every user on these pages is by definition signed out. Wrapping an
 * unauthenticated page in the authenticated shell would produce a redirect
 * loop out of the one page that exists to break one.
 */
export function AuthCard({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
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
            <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
            {subtitle && (
              <p className="mt-1 text-xs leading-relaxed text-ink-faint">{subtitle}</p>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-4 rounded-xl border border-line bg-surface p-6 shadow-[var(--shadow-panel)]">
          {children}
        </div>

        {footer && (
          <p className="mt-5 text-center text-xs leading-relaxed text-ink-faint">
            {footer}
          </p>
        )}
      </div>
    </main>
  );
}

export default AuthCard;
