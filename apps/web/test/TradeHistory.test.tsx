import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import TradeHistory, { fillPriceText } from "@/components/TradeHistory";
import { sessionNavigation } from "@/lib/session";

/**
 * A filled order carries exactly one fill; every other status carries
 * `fills: []`. That is the backend's real shape (D065 + Phase 49/D066),
 * not a convenience of these fixtures.
 */
function order(
  id: string,
  status: string,
  overrides: Partial<Record<string, unknown>> = {},
) {
  const filled = status === "filled";
  return {
    id,
    broker_id: "b-1",
    broker_kind: "paper",
    symbol: "AAPL.US",
    side: "buy",
    quantity: "10.00000000",
    estimated_price: "100.00000000",
    stop_price: "95.00000000",
    status,
    risk_block_reason: null,
    risk_detail: null,
    portfolio_action: filled ? "approve" : null,
    portfolio_binding_constraint: null,
    portfolio_detail: null,
    portfolio_requested_quantity: "10.00000000",
    submitted_by_user_id: "u-1",
    submitted_at: `2026-09-0${id.slice(-1)}T10:00:00Z`,
    fills: filled
      ? [
          {
            id: `f-${id}`,
            order_id: id,
            quantity: "10.00000000",
            fill_price: "100.00000000",
            filled_at: "2026-09-01T10:00:00Z",
          },
        ]
      : [],
    ...overrides,
  };
}

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

function fetchMock() {
  return fetch as unknown as ReturnType<typeof vi.fn>;
}

/** The URL of the nth (0-based) fetch this component issued. */
function requestedUrl(n: number): string {
  return String(fetchMock().mock.calls[n][0]);
}

function submit(brokerId = "b-1", pageSize?: string) {
  fireEvent.change(screen.getByLabelText(/broker id/i), { target: { value: brokerId } });
  if (pageSize !== undefined) {
    fireEvent.change(screen.getByLabelText(/page size/i), { target: { value: pageSize } });
  }
  fireEvent.click(screen.getByRole("button", { name: /load orders/i }));
}

describe("TradeHistory", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders a row for every one of the four real OrderStatus values, each with its own label", async () => {
    mockJson(200, {
      orders: [
        order("o-1", "filled"),
        order("o-2", "rejected", {
          risk_block_reason: "exceeds_max_position_size",
          risk_detail: "position 10000 > limit 5000",
        }),
        order("o-3", "submitted_unconfirmed"),
        order("o-4", "broker_closed_unfilled"),
      ],
      limit: 50,
      offset: 0,
    });

    render(<TradeHistory />);
    submit();

    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(4));

    // Every status is rendered under its own real name. None is collapsed
    // into another, and in particular `rejected` (this system blocked it)
    // and `broker_closed_unfilled` (the venue ended it) stay distinct.
    expect(screen.getByTestId("status-filled")).toHaveTextContent("filled");
    expect(screen.getByTestId("status-rejected")).toHaveTextContent("rejected");
    expect(screen.getByTestId("status-submitted_unconfirmed")).toHaveTextContent(
      "submitted_unconfirmed",
    );
    expect(screen.getByTestId("status-broker_closed_unfilled")).toHaveTextContent(
      "broker_closed_unfilled",
    );

    // The required columns are populated from the real response.
    expect(screen.getAllByText("AAPL.US").length).toBe(4);
    expect(screen.getAllByText("buy").length).toBe(4);
    expect(screen.getAllByText("paper").length).toBe(4);
    expect(screen.getByText("2026-09-01T10:00:00Z")).toBeInTheDocument();
    expect(screen.getByText("2026-09-04T10:00:00Z")).toBeInTheDocument();
  });

  it("shows risk_block_reason only on the row that actually carries one", async () => {
    mockJson(200, {
      orders: [
        order("o-1", "filled"),
        order("o-2", "rejected", { risk_block_reason: "exceeds_max_position_size" }),
      ],
      limit: 50,
      offset: 0,
    });

    render(<TradeHistory />);
    submit();

    await waitFor(() =>
      expect(screen.getByText("exceeds_max_position_size")).toBeInTheDocument(),
    );
    // The filled row has no block reason and is not given a fabricated one.
    expect(screen.getAllByText("exceeds_max_position_size")).toHaveLength(1);
  });

  it("renders a row with no fills without inventing a fill price", async () => {
    mockJson(200, {
      orders: [order("o-2", "rejected"), order("o-1", "filled")],
      limit: 50,
      offset: 0,
    });

    render(<TradeHistory />);
    submit();

    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(2));

    // The unfilled order shows an em-dash, never 0.00 and never a fallback
    // to estimated_price (which is the requested, not the executed, price).
    expect(screen.getByTestId("fill-price-o-2")).toHaveTextContent("—");
    expect(screen.getByTestId("fill-price-o-2")).not.toHaveTextContent("100.00000000");
    // The filled order still shows its real executed price.
    expect(screen.getByTestId("fill-price-o-1")).toHaveTextContent("100.00000000");
  });

  it("pages forward and back by re-requesting the API with real limit/offset", async () => {
    // A full page of 2 → Next is offered.
    mockJson(200, {
      orders: [order("o-1", "filled"), order("o-2", "rejected")],
      limit: 2,
      offset: 0,
    });

    render(<TradeHistory />);
    submit("b-1", "2");

    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(2));
    expect(requestedUrl(0)).toContain("/api/brokers/b-1/orders?limit=2&offset=0");

    // Page 2 comes back short, so Next must stop being offered.
    mockJson(200, { orders: [order("o-3", "filled")], limit: 2, offset: 2 });
    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(1));
    expect(requestedUrl(1)).toContain("limit=2&offset=2");
    expect(screen.getByTestId("page-range")).toHaveTextContent("offset 2");
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();

    // Prev walks back to the real offset 0, it does not re-slice a cache.
    mockJson(200, {
      orders: [order("o-1", "filled"), order("o-2", "rejected")],
      limit: 2,
      offset: 0,
    });
    fireEvent.click(screen.getByRole("button", { name: /prev/i }));

    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(2));
    expect(requestedUrl(2)).toContain("limit=2&offset=0");
    expect(fetchMock()).toHaveBeenCalledTimes(3);
  });

  it("does not offer Prev on the first page", async () => {
    mockJson(200, {
      orders: [order("o-1", "filled"), order("o-2", "rejected")],
      limit: 2,
      offset: 0,
    });

    render(<TradeHistory />);
    submit("b-1", "2");

    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(2));
    expect(screen.getByRole("button", { name: /prev/i })).toBeDisabled();
    // A full page still offers Next, because no total count is returned.
    expect(screen.getByRole("button", { name: /next/i })).not.toBeDisabled();
  });

  it("keeps Prev reachable after paging past the last order", async () => {
    mockJson(200, { orders: [order("o-1", "filled")], limit: 1, offset: 0 });

    render(<TradeHistory />);
    submit("b-1", "1");
    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(1));

    // One page past the end is a real empty page, not an error.
    mockJson(200, { orders: [], limit: 1, offset: 1 });
    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    await waitFor(() =>
      expect(screen.getByText(/recorded no orders on this page/i)).toBeInTheDocument(),
    );
    // The pager must survive the empty page, or the reader is stranded
    // one click beyond the end of the trail with no way back.
    const prev = screen.getByRole("button", { name: /prev/i });
    expect(prev).not.toBeDisabled();
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();

    mockJson(200, { orders: [order("o-1", "filled")], limit: 1, offset: 0 });
    fireEvent.click(prev);

    await waitFor(() => expect(screen.getAllByTestId("order-row")).toHaveLength(1));
    expect(requestedUrl(2)).toContain("limit=1&offset=0");
  });

  it("reports an empty trail as a real empty result, not as a failure", async () => {
    mockJson(200, { orders: [], limit: 50, offset: 0 });

    render(<TradeHistory />);
    submit();

    await waitFor(() =>
      expect(screen.getByText(/recorded no orders/i)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("order-row")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("redirects to login on a 401 instead of showing a raw error", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);

    render(<TradeHistory />);
    submit();

    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByTestId("order-row")).not.toBeInTheDocument();
  });

  it("renders a real 403 for a caller without portfolio:view or without a grant", async () => {
    mockJson(403, { detail: "Missing required permission: portfolio:view" }, false);

    render(<TradeHistory />);
    submit();

    await waitFor(() => expect(screen.getByText(/portfolio:view/)).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 403");
    expect(screen.queryByTestId("order-row")).not.toBeInTheDocument();
  });

  it("renders a real 404 for an unknown broker id", async () => {
    mockJson(404, { detail: "No broker with id b-unknown." }, false);

    render(<TradeHistory />);
    submit("b-unknown");

    await waitFor(() =>
      expect(screen.getByText(/No broker with id b-unknown/)).toBeInTheDocument(),
    );
  });

  it("surfaces the proxy's DATA_UNAVAILABLE sentinel rather than an empty table", async () => {
    mockJson(503, { detail: "DATA_UNAVAILABLE: could not reach the trading API" }, false);

    render(<TradeHistory />);
    submit();

    await waitFor(() => expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument());
    expect(screen.queryByTestId("order-row")).not.toBeInTheDocument();
  });

  it("shows a real error state on network failure", async () => {
    fetchMock().mockRejectedValueOnce(new Error("down"));

    render(<TradeHistory />);
    submit();

    await waitFor(() => expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument());
  });

  it("renders an unrecognised status verbatim instead of bucketing it", async () => {
    mockJson(200, {
      orders: [order("o-9", "some_future_status")],
      limit: 50,
      offset: 0,
    });

    render(<TradeHistory />);
    submit();

    await waitFor(() =>
      expect(screen.getByTestId("status-some_future_status")).toHaveTextContent(
        "some_future_status",
      ),
    );
  });
});

describe("fillPriceText", () => {
  it("returns an em-dash for an order with no fills", () => {
    expect(fillPriceText([])).toBe("—");
  });

  it("returns the executed price for a filled order", () => {
    expect(
      fillPriceText([
        {
          id: "f-1",
          order_id: "o-1",
          quantity: "10",
          fill_price: "100.50",
          filled_at: "t",
        },
      ]),
    ).toBe("100.50");
  });

  it("lists every execution when an order has more than one fill", () => {
    expect(
      fillPriceText([
        { id: "f-1", order_id: "o-1", quantity: "5", fill_price: "100.00", filled_at: "t" },
        { id: "f-2", order_id: "o-1", quantity: "5", fill_price: "101.00", filled_at: "t" },
      ]),
    ).toBe("100.00, 101.00");
  });
});
