import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AlertChannelPanel } from "@/components/admin/AlertsAdmin";

/**
 * The alert-channel check (Phase 111, D138) says "delivered" only when the
 * backend says the channel accepted the message, and otherwise shows the
 * backend's own refusal.
 */

const mockFetch = () => fetch as unknown as ReturnType<typeof vi.fn>;

describe("AlertChannelPanel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports delivery when the channel accepted the message", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ channel: "telegram", delivered: true, detail: "accepted by the channel" }),
    });
    render(<AlertChannelPanel />);
    fireEvent.click(screen.getByRole("button", { name: /send test alert/i }));
    await waitFor(() => expect(screen.getByTestId("alert-test-ok").textContent).toMatch(/telegram accepted/));
    expect(fetch).toHaveBeenCalledWith("/api/admin/alerts/test", { method: "POST" });
  });

  it("shows NOT_CONFIGURED in the backend's words and claims nothing", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({ detail: "NOT_CONFIGURED: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable alerts." }),
    });
    render(<AlertChannelPanel />);
    fireEvent.click(screen.getByRole("button", { name: /send test alert/i }));
    await waitFor(() => expect(screen.getByTestId("alert-test-error").textContent).toMatch(/NOT_CONFIGURED/));
    expect(screen.queryByTestId("alert-test-ok")).toBeNull();
  });

  it("does not report delivery on a 200 that does not say delivered", async () => {
    mockFetch().mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({}) });
    render(<AlertChannelPanel />);
    fireEvent.click(screen.getByRole("button", { name: /send test alert/i }));
    await waitFor(() => expect(screen.getByTestId("alert-test-error")).toBeTruthy());
    expect(screen.queryByTestId("alert-test-ok")).toBeNull();
  });

  it("says so when the API cannot be reached", async () => {
    mockFetch().mockRejectedValueOnce(new Error("network"));
    render(<AlertChannelPanel />);
    fireEvent.click(screen.getByRole("button", { name: /send test alert/i }));
    await waitFor(() => expect(screen.getByTestId("alert-test-error").textContent).toMatch(/DATA_UNAVAILABLE/));
  });
});
