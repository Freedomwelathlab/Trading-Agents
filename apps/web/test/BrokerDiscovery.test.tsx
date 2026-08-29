import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import BrokerDiscovery from "@/components/BrokerDiscovery";
import TradeForm from "@/components/TradeForm";
import { BROKER_SELECTED_EVENT } from "@/lib/brokerSelection";
import { sessionNavigation } from "@/lib/session";

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

const TWO_BROKERS = {
  brokers: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      name: "Alpha Paper",
      kind: "paper",
      provider: "paper-sim",
      is_active: true,
    },
    {
      id: "22222222-2222-2222-2222-222222222222",
      name: "Bravo Paper",
      kind: "paper",
      provider: "paper-sim",
      is_active: false,
    },
  ],
  limit: 50,
  offset: 0,
};

describe("BrokerDiscovery", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the real rows the backend returned for this user", async () => {
    mockJson(200, TWO_BROKERS);
    render(<BrokerDiscovery />);

    await waitFor(() => expect(screen.getByText("Alpha Paper")).toBeInTheDocument());
    expect(screen.getByText("Bravo Paper")).toBeInTheDocument();
    expect(
      screen.getByText("11111111-1111-1111-1111-111111111111"),
    ).toBeInTheDocument();
    // is_active is reported as the backend has it, both ways.
    expect(screen.getByText("true")).toBeInTheDocument();
    expect(screen.getByText("false")).toBeInTheDocument();
  });

  it("requests the listing with the documented limit/offset params", async () => {
    mockJson(200, { brokers: [], limit: 50, offset: 0 });
    render(<BrokerDiscovery />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(fetch).toHaveBeenCalledWith("/api/brokers?limit=50&offset=0");
  });

  it("shows a user with no grants an empty state, not sample brokers", async () => {
    mockJson(200, { brokers: [], limit: 50, offset: 0 });
    render(<BrokerDiscovery />);
    await waitFor(() =>
      expect(screen.getByText(/No brokers on this page/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("redirects to login on a 401 rather than rendering a raw error", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(<BrokerDiscovery />);
    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<BrokerDiscovery />);
    await waitFor(() => expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument());
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the backend's own error detail for a non-401 failure", async () => {
    mockJson(500, { detail: "boom" }, false);
    render(<BrokerDiscovery />);
    await waitFor(() => expect(screen.getByText(/HTTP 500: boom/)).toBeInTheDocument());
  });

  it("broadcasts the selected broker id when Use is clicked", async () => {
    mockJson(200, TWO_BROKERS);
    const seen: string[] = [];
    const handler = (e: Event) => seen.push(String((e as CustomEvent).detail));
    window.addEventListener(BROKER_SELECTED_EVENT, handler);
    try {
      render(<BrokerDiscovery />);
      await waitFor(() => expect(screen.getByText("Alpha Paper")).toBeInTheDocument());
      fireEvent.click(screen.getAllByRole("button", { name: "Use" })[0]);
      expect(seen).toEqual(["11111111-1111-1111-1111-111111111111"]);
      expect(screen.getByRole("button", { name: "In use" })).toBeInTheDocument();
    } finally {
      window.removeEventListener(BROKER_SELECTED_EVENT, handler);
    }
  });

  it("pre-fills the trade form's broker id, end to end", async () => {
    mockJson(200, TWO_BROKERS);
    render(
      <>
        <BrokerDiscovery />
        <TradeForm />
      </>,
    );
    await waitFor(() => expect(screen.getByText("Alpha Paper")).toBeInTheDocument());

    const brokerInput = screen.getByLabelText(/Broker ID/i) as HTMLInputElement;
    expect(brokerInput.value).toBe("");

    fireEvent.click(screen.getAllByRole("button", { name: "Use" })[1]);
    await waitFor(() =>
      expect(brokerInput.value).toBe("22222222-2222-2222-2222-222222222222"),
    );
  });
});
