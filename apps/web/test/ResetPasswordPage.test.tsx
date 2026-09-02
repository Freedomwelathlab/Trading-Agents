import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ResetPasswordPage from "@/app/reset-password/page";

/**
 * Phase 46 / D063. The two properties asserted here are the ones a
 * friendlier implementation would quietly break: the page renders the
 * backend's single INVALID_OR_EXPIRED_TOKEN sentinel rather than guessing
 * a reason, and it never signs anybody in.
 */

let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));

const SENTINEL =
  "INVALID_OR_EXPIRED_TOKEN: this reset link is not valid. Reset links can be used " +
  "once and expire; request a new one.";

function mockJson(status: number, body: unknown) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400,
    status,
    json: async () => body,
  });
}

function submitPasswords(password = "a-new-password-1", confirmation = password) {
  fireEvent.change(screen.getByLabelText(/^new password$/i), {
    target: { value: password },
  });
  fireEvent.change(screen.getByLabelText(/confirm new password/i), {
    target: { value: confirmation },
  });
  fireEvent.click(screen.getByRole("button", { name: /set new password/i }));
}

describe("ResetPasswordPage", () => {
  beforeEach(() => {
    searchParams = new URLSearchParams("token=real-token-abc");
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("confirms success and links to sign in", async () => {
    mockJson(200, { detail: "Password updated. Sign in with your new password." });
    render(<ResetPasswordPage />);
    submitPasswords();

    await waitFor(() => expect(screen.getByTestId("reset-success")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: /go to sign in/i })).toHaveAttribute(
      "href",
      "/login",
    );
  });

  it("renders the INVALID_OR_EXPIRED_TOKEN sentinel verbatim, inventing no reason", async () => {
    mockJson(400, { detail: SENTINEL });
    render(<ResetPasswordPage />);
    submitPasswords();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/INVALID_OR_EXPIRED_TOKEN/);
    // The backend deliberately withholds WHICH failure this was; the page
    // must not fill that in.
    expect(alert).not.toHaveTextContent(/already used/i);
    expect(alert).not.toHaveTextContent(/expired \d/i);
    expect(screen.queryByTestId("reset-success")).toBeNull();
  });

  it("sends the token from the query string with the new password", async () => {
    mockJson(200, { detail: "ok" });
    render(<ResetPasswordPage />);
    submitPasswords("chosen-password-42");

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/auth/password-reset/confirm");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      token: "real-token-abc",
      new_password: "chosen-password-42",
    });
  });

  it("catches a mismatched confirmation client-side without calling the API", async () => {
    render(<ResetPasswordPage />);
    submitPasswords("a-new-password-1", "a-different-password-2");

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/do not match/i),
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("surfaces the backend's own 422 for a too-short password", async () => {
    // The real floor is server-side; the page reports what the server said
    // rather than pre-empting it with a rule of its own.
    mockJson(422, { detail: "String should have at least 8 characters" });
    render(<ResetPasswordPage />);
    submitPasswords("short");

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/at least 8 characters/i),
    );
  });

  it("shows a real DATA_UNAVAILABLE state when the API cannot be reached", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<ResetPasswordPage />);
    submitPasswords();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/DATA_UNAVAILABLE/),
    );
  });

  it("refuses to show a form at all when the URL carries no token", () => {
    searchParams = new URLSearchParams();
    render(<ResetPasswordPage />);

    expect(screen.getByRole("alert")).toHaveTextContent(/needs a reset token in its address/i);
    expect(screen.queryByRole("button", { name: /set new password/i })).toBeNull();
    expect(screen.getByRole("link", { name: /request a new link/i })).toHaveAttribute(
      "href",
      "/forgot-password",
    );
  });

  it("does not sign the user in on success", async () => {
    mockJson(200, { detail: "ok" });
    render(<ResetPasswordPage />);
    submitPasswords();

    await screen.findByTestId("reset-success");
    // Exactly one call — the confirm. Nothing hits /api/auth/login.
    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe("/api/auth/password-reset/confirm");
  });
});
