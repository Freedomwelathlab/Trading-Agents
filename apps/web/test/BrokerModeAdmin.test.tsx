import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  ChangeBrokerModeForm,
  CreateBrokerForm,
} from "@/components/admin/BrokerModeAdmin";

/**
 * Phase 47. The behaviour under test is the D058 safeguard, not the
 * styling: `confirm_live` must reach the backend only when the operator
 * ticked the dedicated checkbox AND the target kind is `live`, and the
 * backend's own 400/409 refusals must be rendered as the backend worded
 * them.
 */

type FetchMock = ReturnType<typeof vi.fn>;

const mockFetch = () => fetch as unknown as FetchMock;

/** The parsed JSON body of the Nth fetch call. */
function bodyOf(call: number): Record<string, unknown> {
  const init = mockFetch().mock.calls[call][1] as RequestInit;
  return JSON.parse(init.body as string) as Record<string, unknown>;
}

const CONFIRM_LABEL = /I understand this designates a REAL, LIVE trading broker/i;

describe("CreateBrokerForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fillRequired() {
    fireEvent.change(screen.getByLabelText(/^name$/i), {
      target: { value: "longport-paper" },
    });
    fireEvent.change(screen.getByLabelText(/^provider$/i), {
      target: { value: "longport" },
    });
  }

  it("creates a paper broker and never sends confirm_live", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({
        id: "b-1",
        name: "longport-paper",
        kind: "paper",
        provider: "longport",
        is_active: false,
      }),
    });

    render(<CreateBrokerForm />);
    fillRequired();
    fireEvent.click(screen.getByRole("button", { name: /create broker/i }));

    await waitFor(() => expect(screen.getByText("b-1")).toBeInTheDocument());
    const body = bodyOf(0);
    expect(body.kind).toBe("paper");
    expect(body).not.toHaveProperty("confirm_live");
  });

  it("leaves the live confirmation unticked and disabled while kind is paper", () => {
    render(<CreateBrokerForm />);
    const box = screen.getByLabelText(CONFIRM_LABEL) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(box.disabled).toBe(true);
  });

  it("submits a live broker WITHOUT confirm_live when the box is unticked, and shows the backend's real 400", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: async () => ({
        detail:
          "LIVE_KIND_CONFIRMATION_REQUIRED: designating a broker as kind='live' makes it eligible for the real-money execution path.",
      }),
    });

    render(<CreateBrokerForm />);
    fillRequired();
    fireEvent.change(screen.getByLabelText(/^kind$/i), { target: { value: "live" } });
    fireEvent.click(screen.getByRole("button", { name: /create broker/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/LIVE_KIND_CONFIRMATION_REQUIRED/),
      ).toBeInTheDocument(),
    );
    const body = bodyOf(0);
    expect(body.kind).toBe("live");
    expect(body).not.toHaveProperty("confirm_live");
  });

  it("sends confirm_live: true only once the box is ticked for a live target", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({
        id: "b-live",
        name: "longport-live",
        kind: "live",
        provider: "longport",
        is_active: false,
      }),
    });

    render(<CreateBrokerForm />);
    fillRequired();
    fireEvent.change(screen.getByLabelText(/^kind$/i), { target: { value: "live" } });
    fireEvent.click(screen.getByLabelText(CONFIRM_LABEL));
    fireEvent.click(screen.getByRole("button", { name: /create broker/i }));

    await waitFor(() => expect(screen.getByText("b-live")).toBeInTheDocument());
    expect(bodyOf(0).confirm_live).toBe(true);
  });

  it("clears a live confirmation when the target kind changes away and back", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: async () => ({ detail: "LIVE_KIND_CONFIRMATION_REQUIRED: ..." }),
    });

    render(<CreateBrokerForm />);
    fillRequired();
    const kind = screen.getByLabelText(/^kind$/i);

    fireEvent.change(kind, { target: { value: "live" } });
    fireEvent.click(screen.getByLabelText(CONFIRM_LABEL));
    expect((screen.getByLabelText(CONFIRM_LABEL) as HTMLInputElement).checked).toBe(
      true,
    );

    // A stale tick must not survive a round trip through "paper".
    fireEvent.change(kind, { target: { value: "paper" } });
    fireEvent.change(kind, { target: { value: "live" } });
    expect((screen.getByLabelText(CONFIRM_LABEL) as HTMLInputElement).checked).toBe(
      false,
    );

    fireEvent.click(screen.getByRole("button", { name: /create broker/i }));
    await waitFor(() => expect(mockFetch()).toHaveBeenCalled());
    expect(bodyOf(0)).not.toHaveProperty("confirm_live");
  });
});

describe("ChangeBrokerModeForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fillBroker(id = "b-1") {
    fireEvent.change(screen.getByLabelText(/broker id/i), { target: { value: id } });
  }

  it("PATCHes the broker-scoped mode path and renders the new kind", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        id: "b-1",
        name: "acct",
        kind: "paper",
        provider: "longport",
        is_active: true,
      }),
    });

    render(<ChangeBrokerModeForm />);
    fillBroker();
    fireEvent.click(screen.getByRole("button", { name: /change mode/i }));

    await waitFor(() => expect(screen.getByText("b-1")).toBeInTheDocument());
    expect(mockFetch().mock.calls[0][0]).toBe("/api/admin/brokers/b-1/mode");
    expect((mockFetch().mock.calls[0][1] as RequestInit).method).toBe("PATCH");
    expect(bodyOf(0)).toEqual({ kind: "paper" });
  });

  it("renders the backend's real 409 for a broker that already has recorded orders", async () => {
    const detail =
      "Broker b-1 has recorded orders and its kind can no longer be changed. The orders/fills history is append-only and does not record a per-order kind, so flipping it would make simulated and real trades indistinguishable in the audit trail. Create a new broker instead.";
    mockFetch().mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail }),
    });

    render(<ChangeBrokerModeForm />);
    fillBroker();
    fireEvent.click(screen.getByRole("button", { name: /change mode/i }));

    await waitFor(() =>
      expect(screen.getByText(/has recorded orders/)).toBeInTheDocument(),
    );
    // Verbatim, not a friendlier paraphrase. Asserted against the alert
    // itself rather than the document, because the panel's own standing
    // explanation also ends with the same remedy sentence.
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("HTTP 409");
    expect(alert.textContent).toContain(detail);
  });

  it("sends confirm_live: true when flipping to live with the box ticked", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        id: "b-2",
        name: "acct",
        kind: "live",
        provider: "longport",
        is_active: true,
      }),
    });

    render(<ChangeBrokerModeForm />);
    fillBroker("b-2");
    fireEvent.change(screen.getByLabelText(/target kind/i), {
      target: { value: "live" },
    });
    fireEvent.click(screen.getByLabelText(CONFIRM_LABEL));
    fireEvent.click(screen.getByRole("button", { name: /change mode/i }));

    await waitFor(() => expect(screen.getByText("b-2")).toBeInTheDocument());
    expect(bodyOf(0)).toEqual({ kind: "live", confirm_live: true });
  });

  it("surfaces a real 403 for a non-admin caller", async () => {
    mockFetch().mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: "Missing required permission: admin:manage" }),
    });

    render(<ChangeBrokerModeForm />);
    fillBroker();
    fireEvent.click(screen.getByRole("button", { name: /change mode/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/Missing required permission: admin:manage/),
      ).toBeInTheDocument(),
    );
  });
});
