import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { CreateRoleForm, UpdateRoleForm } from "@/components/admin/RolesAdmin";

describe("CreateRoleForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a created role's permissions on success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({
        id: "r-1",
        name: "trader",
        description: null,
        permissions: ["trade:submit:paper"],
      }),
    });

    render(<CreateRoleForm />);
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "trader" } });
    fireEvent.change(screen.getByLabelText(/permissions/i), {
      target: { value: "trade:submit:paper" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create role/i }));

    await waitFor(() =>
      expect(screen.getByText("trade:submit:paper")).toBeInTheDocument(),
    );
  });

  it("surfaces a real 409 for a duplicate role name", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: "Role with this name already exists" }),
    });

    render(<CreateRoleForm />);
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "trader" } });
    fireEvent.click(screen.getByRole("button", { name: /create role/i }));

    await waitFor(() =>
      expect(screen.getByText(/already exists/)).toBeInTheDocument(),
    );
  });
});

describe("UpdateRoleForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders updated permissions on success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        id: "r-1",
        name: "trader",
        description: "updated",
        permissions: ["trade:submit:paper", "admin:manage"],
      }),
    });

    render(<UpdateRoleForm />);
    fireEvent.change(screen.getByLabelText(/role id/i), { target: { value: "r-1" } });
    fireEvent.click(screen.getByLabelText(/replace permissions with/i));
    fireEvent.click(screen.getByRole("button", { name: /update role/i }));

    await waitFor(() =>
      expect(
        screen.getByText("trade:submit:paper, admin:manage"),
      ).toBeInTheDocument(),
    );
  });

  it("surfaces a real 403 for a non-admin caller", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: "Missing required permission: admin:manage" }),
    });

    render(<UpdateRoleForm />);
    fireEvent.change(screen.getByLabelText(/role id/i), { target: { value: "r-1" } });
    fireEvent.click(screen.getByRole("button", { name: /update role/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/Missing required permission: admin:manage/),
      ).toBeInTheDocument(),
    );
  });
});
