"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/primitives";
import { SESSION_ENDPOINT, SessionInfo, handleExpiredSession } from "@/lib/session";

/**
 * The Administration page's permission notice, shown only when it is true
 * (Phase 84). Until now the warning rendered for everyone, including the
 * owner, which read as a standing error. It reads the real session's
 * permission list; the backend still enforces `admin:manage` on every
 * request regardless of what this component decides to show.
 */
export default function AdminGate() {
  const [permissions, setPermissions] = useState<string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(async () => {
      try {
        const res = await fetch(SESSION_ENDPOINT, { cache: "no-store" });
        if (handleExpiredSession(res.status)) return;
        const data = (await res.json().catch(() => null)) as SessionInfo | null;
        if (!cancelled) setPermissions(data?.permissions ?? []);
      } catch {
        if (!cancelled) setPermissions([]);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (permissions === null || permissions.includes("admin:manage")) return null;
  return (
    <Alert tone="warn" role="status" className="max-w-3xl" testId="admin-gate">
      Your account does not hold <code className="font-mono">admin:manage</code>. Every
      action on this page will return a real 403 from the backend when submitted.
    </Alert>
  );
}
