import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  CreateUserForm,
  ResetUserPasswordForm,
  UpdateUserForm,
} from "@/components/admin/UsersAdmin";

describe("CreateUserForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fillRequired() {
    fireEvent.change(screen.getByLabelText(/^email$/i), {
      target: { value: "new.user@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/^password$/i), {
      target: { value: "supersecret" },
    });
  }

  it("renders a created user on success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({
        id: "u-1",
        email: "new.user@example.com",
        is_active: true,
        role_id: null,
      }),
    });

    render(<CreateUserForm />);
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: /create user/i }));

    await waitFor(() =>
      expect(screen.getByText("new.user@example.com")).toBeInTheDocument(),
    );
    expect(screen.getByText("u-1")).toBeInTheDocument();
  });

  it("renders a real 403 for a non-admin caller, not a hidden/generic message", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: "Missing required permission: admin:manage" }),
    });

    render(<CreateUserForm />);
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: /create user/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/Missing required permission: admin:manage/),
      ).toBeInTheDocument(),
    );
  });

  it("renders a real 409 for a duplicate email", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: "User with this email already exists" }),
    });

    render(<CreateUserForm />);
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: /create user/i }));

    await waitFor(() =>
      expect(screen.getByText(/already exists/)).toBeInTheDocument(),
    );
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("down"),
    );

    render(<CreateUserForm />);
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: /create user/i }));

    await waitFor(() =>
      expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument(),
    );
  });
});

describe("UpdateUserForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the updated user on success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        id: "u-1",
        email: "existing@example.com",
        is_active: false,
        role_id: null,
      }),
    });

    render(<UpdateUserForm />);
    fireEvent.change(screen.getByLabelText(/user id/i), {
      target: { value: "u-1" },
    });
    fireEvent.click(screen.getByLabelText(/change active status to/i));
    fireEvent.click(screen.getByRole("button", { name: /update user/i }));

    await waitFor(() => expect(screen.getByText("false")).toBeInTheDocument());
  });

  it("surfaces a real 404 for an unknown user_id", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ detail: "User not found" }),
    });

    render(<UpdateUserForm />);
    fireEvent.change(screen.getByLabelText(/user id/i), {
      target: { value: "does-not-exist" },
    });
    fireEvent.click(screen.getByRole("button", { name: /update user/i }));

    await waitFor(() =>
      expect(screen.getByText(/User not found/)).toBeInTheDocument(),
    );
  });
});

/**
 * Phase 46 / D063. The two delivery branches must stay visibly different:
 * conflating them would either hide a live credential an admin needs, or
 * imply an email was sent when none was.
 */
describe("ResetUserPasswordForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function mockJson(status: number, body: unknown) {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: status < 400,
      status,
      json: async () => body,
    });
  }

  function submit(userId = "u-1") {
    fireEvent.change(screen.getByLabelText(/reset user id/i), {
      target: { value: userId },
    });
    fireEvent.click(screen.getByRole("button", { name: /issue reset link/i }));
  }

  it("shows the real reset link when delivery is NOT_CONFIGURED", async () => {
    mockJson(201, {
      user_id: "u-1",
      expires_at: "2026-09-02T12:30:00Z",
      delivery: "NOT_CONFIGURED_returned_directly",
      reset_link: "http://localhost:3000/reset-password?token=abc123",
    });

    render(<ResetUserPasswordForm />);
    submit();

    await waitFor(() =>
      expect(screen.getByTestId("reset-link")).toHaveTextContent(
        "http://localhost:3000/reset-password?token=abc123",
      ),
    );
    expect(screen.getByText(/NOT_CONFIGURED_returned_directly/)).toBeInTheDocument();
    // The admin is told what they are holding, not just handed a URL.
    expect(screen.getByText(/live, single-use credential/i)).toBeInTheDocument();
  });

  it("confirms a send WITHOUT showing a link when a provider is configured", async () => {
    mockJson(201, {
      user_id: "u-1",
      expires_at: "2026-09-02T12:30:00Z",
      delivery: "SENT",
      reset_link: null,
    });

    render(<ResetUserPasswordForm />);
    submit();

    await waitFor(() => expect(screen.getByText("SENT")).toBeInTheDocument());
    expect(screen.queryByTestId("reset-link")).toBeNull();
    // "accepted", never "delivered" — nothing here can see an inbox.
    expect(screen.getByText(/accepted a message/i)).toBeInTheDocument();
  });

  it("renders a real 403 for a caller without admin:manage", async () => {
    mockJson(403, { detail: "Missing required permission: admin:manage" });

    render(<ResetUserPasswordForm />);
    submit();

    await waitFor(() =>
      expect(
        screen.getByText(/Missing required permission: admin:manage/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("reset-link")).toBeNull();
  });

  it("renders a real 502 when the configured provider refused the message", async () => {
    mockJson(502, {
      detail:
        "EMAIL_DELIVERY_FAILED: the configured email provider refused the message, " +
        "so no reset link reached user@example.com. The token issued for this " +
        "attempt has been invalidated.",
    });

    render(<ResetUserPasswordForm />);
    submit();

    await waitFor(() =>
      expect(screen.getByText(/EMAIL_DELIVERY_FAILED/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/HTTP 502/);
  });

  it("surfaces a real 404 for an unknown user id", async () => {
    mockJson(404, { detail: "No user with id u-nope." });

    render(<ResetUserPasswordForm />);
    submit("u-nope");

    await waitFor(() =>
      expect(screen.getByText(/No user with id u-nope/)).toBeInTheDocument(),
    );
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("down"),
    );

    render(<ResetUserPasswordForm />);
    submit();

    await waitFor(() =>
      expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument(),
    );
  });

  it("posts to the per-user admin reset endpoint", async () => {
    mockJson(201, { user_id: "u-1", delivery: "SENT", reset_link: null });

    render(<ResetUserPasswordForm />);
    submit("u-1");

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/admin/users/u-1/password-reset");
    expect((init as RequestInit).method).toBe("POST");
  });
});
