import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import BrokerAccountsAdmin from "@/components/admin/BrokerAccountsAdmin";

function mockJson(status: number, body: unknown) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400,
    status,
    json: async () => body,
  });
}

const ROWS = {
  brokers: [
    {
      id: "aaaaaaaa-0000-0000-0000-000000000001",
      name: "Old test paper",
      kind: "paper",
      provider: "paper",
      approved: true,
      adapter_status: "built_in",
      created_at: "2026-09-20T00:00:00Z",
      order_count: 0,
      running_bots: 0,
      running_deployments: 0,
    },
    {
      id: "aaaaaaaa-0000-0000-0000-000000000002",
      name: "Traded paper",
      kind: "paper",
      provider: "paper",
      approved: false,
      adapter_status: "built_in",
      created_at: "2026-09-19T00:00:00Z",
      order_count: 12,
      running_bots: 1,
      running_deployments: 0,
    },
  ],
};

describe("BrokerAccountsAdmin", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists every broker with its desk status and what is running on it", async () => {
    mockJson(200, ROWS);
    render(<BrokerAccountsAdmin />);
    await waitFor(() => expect(screen.getByText("Old test paper")).toBeInTheDocument());
    // Once in the description's <strong>, once as the row's pill.
    expect(screen.getAllByText("approved")).toHaveLength(2);
    expect(screen.getByText("hidden")).toBeInTheDocument();
    expect(screen.getByText("1 bot · 0 deploy")).toBeInTheDocument();
  });

  it("actions stay disabled until a broker is ticked", async () => {
    mockJson(200, ROWS);
    render(<BrokerAccountsAdmin />);
    await waitFor(() => expect(screen.getByText("Old test paper")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Select Old test paper"));
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeEnabled();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("select-all ticks every row", async () => {
    mockJson(200, ROWS);
    render(<BrokerAccountsAdmin />);
    await waitFor(() => expect(screen.getByText("Old test paper")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("Select all brokers"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
  });

  it("approving posts the ticked ids and reloads", async () => {
    mockJson(200, ROWS);
    render(<BrokerAccountsAdmin />);
    await waitFor(() => expect(screen.getByText("Traded paper")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("Select Traded paper"));

    mockJson(200, { updated: 1, not_found: [] });
    mockJson(200, ROWS);
    fireEvent.click(screen.getByRole("button", { name: "Approve for desk" }));

    await waitFor(() =>
      expect(screen.getByText(/1 broker\(s\) approved for the trading desk/)).toBeInTheDocument(),
    );
    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[1];
    expect(call[0]).toBe("/api/admin/brokers/approval");
    expect(JSON.parse(call[1].body)).toEqual({
      broker_ids: ["aaaaaaaa-0000-0000-0000-000000000002"],
      approved: true,
    });
  });

  it("delete asks first, then shows the backend's per-broker outcome verbatim", async () => {
    mockJson(200, ROWS);
    render(<BrokerAccountsAdmin />);
    await waitFor(() => expect(screen.getByText("Old test paper")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("Select all brokers"));

    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    expect(confirm).toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1); // declined: nothing sent

    confirm.mockReturnValueOnce(true);
    mockJson(200, {
      results: [
        {
          broker_id: "aaaaaaaa-0000-0000-0000-000000000001",
          name: "Old test paper",
          outcome: "deleted",
          detail: "Deleted, with its paper book, snapshots, credentials and grants.",
        },
        {
          broker_id: "aaaaaaaa-0000-0000-0000-000000000002",
          name: "Traded paper",
          outcome: "refused",
          detail: "1 bot(s) and 0 strategy deployment(s) on this broker are not stopped.",
        },
      ],
    });
    mockJson(200, { brokers: [ROWS.brokers[1]] });
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    await waitFor(() => expect(screen.getByTestId("delete-outcomes")).toBeInTheDocument());
    expect(screen.getByText("deleted")).toBeInTheDocument();
    expect(screen.getByText("refused")).toBeInTheDocument();
    expect(screen.getByText(/are not stopped/)).toBeInTheDocument();
  });
});
