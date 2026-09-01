"use client";

import { useRouter } from "next/navigation";
import { btnSecondary } from "@/components/ui/primitives";

export default function LogoutButton() {
  const router = useRouter();

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
    router.refresh();
  }

  return (
    <button onClick={handleLogout} className={`${btnSecondary} w-full`}>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-4 w-4"
        aria-hidden="true"
      >
        <path d="M14.5 8.5V6.5a1.5 1.5 0 0 0-1.5-1.5H6.5A1.5 1.5 0 0 0 5 6.5v11A1.5 1.5 0 0 0 6.5 19H13a1.5 1.5 0 0 0 1.5-1.5v-2" />
        <path d="M10 12h9m0 0-2.5-2.5M19 12l-2.5 2.5" />
      </svg>
      Sign out
    </button>
  );
}
