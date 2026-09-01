import type { ReactNode } from "react";
import SideNav from "@/components/shell/SideNav";
import SessionStatus from "@/components/SessionStatus";
import LogoutButton from "@/components/LogoutButton";

/**
 * The dashboard shell (Phase 45 / D060): a persistent left rail carrying
 * brand, navigation and the real session state, plus a sticky page header
 * for the title and per-page status strip.
 *
 * Desktop-primary by design — this is an operator tool, and the panels it
 * frames are wide numeric tables. The rail collapses to a stacked header
 * below `lg` so nothing is unreachable on a small laptop or in portrait,
 * but the layout is tuned for 1280px and up.
 *
 * The shell renders no backend data of its own. `SessionStatus` is the
 * same component as before and still polls `GET /auth/session`; it is
 * merely placed somewhere permanent instead of being buried under a page
 * title.
 */
export default function AppShell({
  title,
  subtitle,
  statusStrip,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  /** Real, backend-driven status rendered under the page title. */
  statusStrip?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-accent focus:px-4 focus:py-2 focus:text-sm focus:font-semibold focus:text-accent-ink"
      >
        Skip to main content
      </a>

      {/* ---- Left rail ---- */}
      <aside className="flex shrink-0 flex-col gap-6 border-b border-line bg-surface px-4 py-4 lg:h-screen lg:w-60 lg:border-b-0 lg:border-r lg:sticky lg:top-0 lg:py-6">
        <BrandMark />
        <SideNav />
        <div className="mt-auto flex flex-col gap-3 border-t border-line pt-4">
          <SessionStatus />
          <LogoutButton />
        </div>
      </aside>

      {/* ---- Content column ---- */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-line bg-canvas/85 px-5 py-4 backdrop-blur-md xl:px-8">
          <div className="mx-auto flex max-w-[1600px] flex-col gap-3">
            <div>
              <h1 className="text-xl font-semibold tracking-tight text-ink">
                {title}
              </h1>
              {subtitle && (
                <p className="mt-1 max-w-prose text-sm text-ink-muted">{subtitle}</p>
              )}
            </div>
            {statusStrip}
          </div>
        </header>

        <main
          id="main"
          className="mx-auto w-full max-w-[1600px] flex-1 px-5 py-6 xl:px-8"
        >
          {children}
        </main>
      </div>
    </div>
  );
}

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5 px-1">
      <svg
        viewBox="0 0 28 28"
        className="h-7 w-7 shrink-0"
        role="img"
        aria-label="Trading OS"
      >
        <rect
          x="1"
          y="1"
          width="26"
          height="26"
          rx="7"
          className="fill-accent/12 stroke-accent/50"
          strokeWidth="1"
        />
        {/* A candle and an up-leg: the mark is the product, not decoration. */}
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
      <div className="min-w-0 leading-tight">
        <p className="truncate text-sm font-semibold tracking-tight text-ink">
          Trading OS
        </p>
        <p className="truncate font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
          Operator console
        </p>
      </div>
    </div>
  );
}
