import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ForgotPasswordPage from "@/app/forgot-password/page";

/**
 * Phase 46 / D063. The property under test throughout is that this page
 * renders the BACKEND's own words and never substitutes a friendlier
 * claim of its own — the anti-enumeration guarantee is only as strong as
 * its weakest renderer.
 */

const ACK =
  "If that email address is registered, a password reset link has been issued for it. " +
  "The link can be used once and expires shortly. If no email arrives, contact an " +
  "administrator - this deployment may not have email delivery configured.";

function mockJson(status: number, body: unknown) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400,
    status,
    json: async () => body,
  });
}

function submitEmail(value = "trader@example.com") {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));
}

describe("ForgotPasswordPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the backend's generic acknowledgement verbatim", async () => {
    mockJson(200, { detail: ACK });
    render(<ForgotPasswordPage />);
    submitEmail();

    await waitFor(() =>
      expect(screen.getByTestId("reset-acknowledgement")).toHaveTextContent(
        /a password reset link has been issued for it/i,
      ),
    );
  });

  it("never claims an email was sent, because the backend does not", async () => {
    mockJson(200, { detail: ACK });
    render(<ForgotPasswordPage />);
    submitEmail();

    const note = await screen.findByTestId("reset-acknowledgement");
    expect(note).not.toHaveTextContent(/check your inbox/i);
    expect(note).not.toHaveTextContent(/we have emailed/i);
    // ...and it keeps the backend's honest escape hatch for the
    // NOT_CONFIGURED case rather than dropping it.
    expect(note).toHaveTextContent(/contact an administrator/i);
  });

  it("shows the SAME acknowledgement for a registered and an unknown address", async () => {
    // The backend returns one body for both; this asserts the page does not
    // manufacture a difference the API deliberately withheld.
    mockJson(200, { detail: ACK });
    const first = render(<ForgotPasswordPage />);
    submitEmail("real@example.com");
    const registered = (await screen.findByTestId("reset-acknowledgement")).textContent;
    first.unmount();

    mockJson(200, { detail: ACK });
    render(<ForgotPasswordPage />);
    submitEmail("nobody@example.com");
    const unknown = (await screen.findByTestId("reset-acknowledgement")).textContent;

    expect(registered).toBe(unknown);
  });

  it("renders a real 429 from the throttle as an error, not as an acknowledgement", async () => {
    mockJson(429, {
      detail: "Too many password reset requests from this address. Try again later.",
    });
    render(<ForgotPasswordPage />);
    submitEmail();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/too many password reset requests/i),
    );
    expect(screen.queryByTestId("reset-acknowledgement")).toBeNull();
  });

  it("shows a real DATA_UNAVAILABLE state when the API cannot be reached", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<ForgotPasswordPage />);
    submitEmail();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/DATA_UNAVAILABLE/),
    );
  });

  it("posts the email to the password-reset request route handler", async () => {
    mockJson(200, { detail: ACK });
    render(<ForgotPasswordPage />);
    submitEmail("trader@example.com");

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/auth/password-reset/request");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      email: "trader@example.com",
    });
  });

  it("keeps the form usable after a successful submit", async () => {
    // A user on a deployment with no email provider has to be able to read
    // the message and act on it, not be dropped at a dead end.
    mockJson(200, { detail: ACK });
    render(<ForgotPasswordPage />);
    submitEmail();

    await screen.findByTestId("reset-acknowledgement");
    expect(screen.getByRole("button", { name: /send reset link/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
  });

  it("offers a way back to sign in", () => {
    render(<ForgotPasswordPage />);
    expect(screen.getByRole("link", { name: /back to sign in/i })).toHaveAttribute(
      "href",
      "/login",
    );
  });
});
