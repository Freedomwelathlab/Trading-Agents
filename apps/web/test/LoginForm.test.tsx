import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import LoginPage from "@/app/login/page";

const push = vi.fn();
const refresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
  useSearchParams: () => new URLSearchParams(),
}));

function mockJson(status: number, body: unknown) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400,
    status,
    json: async () => body,
  });
}

function submitCredentials() {
  fireEvent.change(screen.getByLabelText(/email/i), {
    target: { value: "trader@example.com" },
  });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "hunter2" } });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    push.mockClear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the backend's real lockout message on a 423, not a generic failure", async () => {
    // Phase 39 / D049: the backend locks an account after N consecutive
    // failed attempts and answers a subsequent CORRECT password with 423.
    // The user must be told that is what happened - "Incorrect email or
    // password" would send them hunting for a typo that isn't there.
    mockJson(423, {
      detail:
        "Account temporarily locked after repeated failed login attempts. Try again later.",
    });
    render(<LoginPage />);
    submitCredentials();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/temporarily locked/i),
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent(/incorrect email or password/i);
    expect(push).not.toHaveBeenCalled();
  });

  it("still renders the generic 401 verbatim, so the two cases stay distinguishable", async () => {
    mockJson(401, { detail: "Incorrect email or password." });
    render(<LoginPage />);
    submitCredentials();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/incorrect email or password/i),
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent(/locked/i);
  });

  it("navigates to the dashboard on success", async () => {
    mockJson(200, { ok: true });
    render(<LoginPage />);
    submitCredentials();

    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
  });

  it("always offers the forgot-password route, not only after a failure", () => {
    // Phase 46 / D063: a user who cannot log in should not have to fail
    // first to discover that a reset exists.
    render(<LoginPage />);
    expect(screen.getByRole("link", { name: /forgot password/i })).toHaveAttribute(
      "href",
      "/forgot-password",
    );
  });
});
