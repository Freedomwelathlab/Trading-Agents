import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  CreateBrokerGrantForm,
  DeleteBrokerGrantForm,
} from "@/components/admin/BrokerGrantsAdmin";

describe("CreateBrokerGrantForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fill() {
    fireEvent.change(screen.getByLabelText(/user id/i), { target: { value: "u-1" } });
    fireEvent.change(screen.getByLabelText(/broker id/i), { target: { value: "b-1" } });
  }

  it("renders a created grant on success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({ id: "g-1", user_id: "u-1", broker_id: "b-1" }),
    });

    render(<CreateBrokerGrantForm />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /create grant/i }));

    await waitFor(() => expect(screen.getByText("g-1")).toBeInTheDocument());
  });

  it("surfaces a real 409 for a duplicate grant", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: "Grant already exists" }),
    });

    render(<CreateBrokerGrantForm />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /create grant/i }));

    await waitFor(() =>
      expect(screen.getByText(/Grant already exists/)).toBeInTheDocument(),
    );
  });

  it("surfaces a real 404 when the user or broker doesn't exist", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ detail: "User not found" }),
    });

    render(<CreateBrokerGrantForm />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: /create grant/i }));

    await waitFor(() =>
      expect(screen.getByText(/User not found/)).toBeInTheDocument(),
    );
  });
});

describe("DeleteBrokerGrantForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("confirms deletion on a real 204", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 204,
      json: async () => null,
    });

    render(<DeleteBrokerGrantForm />);
    fireEvent.change(screen.getByLabelText(/grant id/i), { target: { value: "g-1" } });
    fireEvent.click(screen.getByRole("button", { name: /delete grant/i }));

    await waitFor(() =>
      expect(screen.getByText(/revoked/i)).toBeInTheDocument(),
    );
  });

  it("surfaces a real 404 for an unknown grant, never a fabricated success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Grant not found" }),
    });

    render(<DeleteBrokerGrantForm />);
    fireEvent.change(screen.getByLabelText(/grant id/i), { target: { value: "g-nope" } });
    fireEvent.click(screen.getByRole("button", { name: /delete grant/i }));

    await waitFor(() =>
      expect(screen.getByText(/Grant not found/)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/revoked/i)).not.toBeInTheDocument();
  });

  it("surfaces a real 403 for a non-admin caller", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: "Missing required permission: admin:manage" }),
    });

    render(<DeleteBrokerGrantForm />);
    fireEvent.change(screen.getByLabelText(/grant id/i), { target: { value: "g-1" } });
    fireEvent.click(screen.getByRole("button", { name: /delete grant/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/Missing required permission: admin:manage/),
      ).toBeInTheDocument(),
    );
  });
});
