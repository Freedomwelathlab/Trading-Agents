import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { CreateUserForm, UpdateUserForm } from "@/components/admin/UsersAdmin";

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
