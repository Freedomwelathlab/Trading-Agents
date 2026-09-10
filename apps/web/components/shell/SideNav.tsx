"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * Primary navigation for the dashboard shell (Phase 45 / D060).
 *
 * Both destinations are always shown. `/admin` is deliberately NOT hidden
 * for non-admins: permission is never guessed client-side, and hiding the
 * link would imply a security boundary that does not exist at this layer.
 * The real `admin:manage` gate is the backend's, and a user without it
 * sees its real 403 when they submit an admin form (see the note on
 * `app/admin/page.tsx`).
 */

type Item = {
  href: "/dashboard" | "/strategies" | "/strategies/leaderboard" | "/admin";
  label: string;
  icon: React.ReactNode;
};

function IconTerminal() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4 shrink-0"
      aria-hidden="true"
    >
      <path d="M3 5.5h18v13H3z" />
      <path d="m7 10 2.5 2L7 14" />
      <path d="M12.5 14.5H17" />
    </svg>
  );
}

function IconFlask() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4 shrink-0"
      aria-hidden="true"
    >
      <path d="M10 3h4" />
      <path d="M10 3v5.5L4.8 18a1.5 1.5 0 0 0 1.3 2.3h11.8a1.5 1.5 0 0 0 1.3-2.3L14 8.5V3" />
      <path d="M7.5 15h9" />
    </svg>
  );
}

function IconTrophy() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4 shrink-0"
      aria-hidden="true"
    >
      <path d="M7 4h10v5a5 5 0 0 1-10 0z" />
      <path d="M7 5H4v1.5A3.5 3.5 0 0 0 7.5 10" />
      <path d="M17 5h3v1.5A3.5 3.5 0 0 1 16.5 10" />
      <path d="M12 14v3" />
      <path d="M9 20.5h6" />
      <path d="M9.5 17.5h5l.5 3h-6z" />
    </svg>
  );
}

function IconShield() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4 shrink-0"
      aria-hidden="true"
    >
      <path d="M12 3.5 5 6v5.5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9V6z" />
      <path d="m9.5 12 1.8 1.8L15 10" />
    </svg>
  );
}

const ITEMS: Item[] = [
  { href: "/dashboard", label: "Trading desk", icon: <IconTerminal /> },
  { href: "/strategies", label: "Strategy Lab", icon: <IconFlask /> },
  { href: "/strategies/leaderboard", label: "Leaderboard", icon: <IconTrophy /> },
  { href: "/admin", label: "Administration", icon: <IconShield /> },
];

export default function SideNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Primary" className="flex flex-col gap-1">
      {ITEMS.map((item) => {
        const active = pathname === item.href;
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={
              "group flex items-center gap-2.5 rounded-md px-3 py-2 text-sm " +
              "transition-colors duration-150 focus-visible:outline-2 " +
              "focus-visible:outline-offset-2 focus-visible:outline-accent " +
              (active
                ? "bg-accent/12 font-semibold text-accent"
                : "text-ink-muted hover:bg-well hover:text-ink")
            }
          >
            <span
              aria-hidden="true"
              className={
                "h-4 w-[2px] rounded-full transition-colors " +
                (active ? "bg-accent" : "bg-transparent")
              }
            />
            {item.icon}
            <span className="truncate">{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
