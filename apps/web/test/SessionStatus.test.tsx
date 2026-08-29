import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import SessionStatus from "@/components/SessionStatus";
import { formatRemaining, sessionNavigation } from "@/lib/session";

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

const SESSION = {
  user_id: "u-1",
  email: "trader@example.com",
  issued_at: "2026-08-29T00:00:00Z",
  expires_at: "2026-08-29T00:30:00Z",
  expires_in_seconds: 1500,
};

describe("SessionStatus", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the backend's real remaining time, not a locally guessed one", async () => {
    mockJson(200, SESSION);
    render(<SessionStatus />);

    await waitFor(() =>
      expect(screen.getByTestId("session-status")).toHaveTextContent("trader@example.com"),
    );
    // 1500s === 25m, exactly what the server reported.
    expect(screen.getByTestId("session-status")).toHaveTextContent("25m left");
    expect(screen.getByTestId("session-status")).not.toHaveTextContent(/expiring soon/i);
    expect(fetch).toHaveBeenCalledWith("/api/auth/session", { cache: "no-store" });
  });

  it("warns when the server says the session is expiring soon", async () => {
    mockJson(200, { ...SESSION, expires_in_seconds: 120 });
    render(<SessionStatus />);

    await waitFor(() =>
      expect(screen.getByTestId("session-status")).toHaveTextContent(/Session expiring soon/i),
    );
    expect(screen.getByTestId("session-status")).toHaveTextContent("2m left");
  });

  it("redirects to login when the session poll comes back 401", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);

    render(<SessionStatus />);

    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByTestId("session-status")).not.toBeInTheDocument();
  });

  it("reports a non-401 failure honestly instead of showing a fake session", async () => {
    mockJson(503, { detail: "DATA_UNAVAILABLE: could not reach the trading API" }, false);
    render(<SessionStatus />);

    await waitFor(() =>
      expect(screen.getByTestId("session-status")).toHaveTextContent(/DATA_UNAVAILABLE/),
    );
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<SessionStatus />);

    await waitFor(() =>
      expect(screen.getByTestId("session-status")).toHaveTextContent(
        /Session status unavailable/i,
      ),
    );
  });
});

describe("formatRemaining", () => {
  it("formats seconds, minutes, and hours without rounding up a dead session", () => {
    expect(formatRemaining(0)).toBe("expired");
    expect(formatRemaining(-5)).toBe("expired");
    expect(formatRemaining(45)).toBe("45s");
    expect(formatRemaining(90)).toBe("1m");
    expect(formatRemaining(3600)).toBe("1h 0m");
    expect(formatRemaining(3900)).toBe("1h 5m");
  });
});
