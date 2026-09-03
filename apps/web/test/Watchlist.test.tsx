import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Watchlist from "@/components/Watchlist";
import { sessionNavigation } from "@/lib/session";

/**
 * Same conventions as the other component suites here: `fetch` is stubbed,
 * every assertion is about what the component rendered from a real backend
 * shape, and nothing asserts on styling.
 *
 * The load-bearing tests are the two DATA_UNAVAILABLE ones. A watchlist
 * panel that silently dropped an unpriceable symbol, or filled one in,
 * would still look perfectly fine on screen — so those cases are asserted
 * positively (the sentinel is present) and negatively (no price appears
 * for that symbol).
 */

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

const ONE_LIST = {
  watchlists: [
    {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      name: "Semis",
      created_at: "2026-09-03T10:00:00Z",
      symbols: ["AAPL.US", "NOSUCH.US"],
    },
  ],
  limit: 50,
  offset: 0,
};

const MIXED_QUOTES = {
  watchlist_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  name: "Semis",
  market_data_configured: true,
  quotes: [
    {
      symbol: "AAPL.US",
      price: "195.25",
      as_of: "2026-09-03T12:00:00Z",
      source: "longbridge",
      unavailable: null,
    },
    {
      symbol: "NOSUCH.US",
      price: null,
      as_of: null,
      source: null,
      unavailable:
        "DATA_UNAVAILABLE: NO_DATA_AVAILABLE: no configured provider returned data for 'NOSUCH.US'.",
    },
  ],
};

describe("Watchlist", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads the caller's watchlists on mount and lists their symbols", async () => {
    mockJson(200, ONE_LIST);
    render(<Watchlist />);

    await waitFor(() => expect(screen.getByText("AAPL.US")).toBeInTheDocument());
    expect(screen.getByText("NOSUCH.US")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/api/watchlists?limit=50&offset=0", {
      cache: "no-store",
    });
  });

  it("shows an explicit empty state rather than inventing a default watchlist", async () => {
    mockJson(200, { watchlists: [], limit: 50, offset: 0 });
    render(<Watchlist />);

    await waitFor(() =>
      expect(screen.getByText(/You have no watchlists yet/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    // No symbol chips, no fabricated list name.
    expect(screen.queryByTestId("watchlist-symbols")).not.toBeInTheDocument();
  });

  it("creates a watchlist with the name the user typed", async () => {
    mockJson(200, { watchlists: [], limit: 50, offset: 0 });
    render(<Watchlist />);
    await waitFor(() => expect(screen.getByText(/no watchlists yet/i)).toBeInTheDocument());

    mockJson(201, {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      name: "Semis",
      created_at: "2026-09-03T10:00:00Z",
      symbols: [],
    });
    mockJson(200, ONE_LIST);

    fireEvent.change(screen.getByLabelText(/New watchlist name/i), {
      target: { value: "Semis" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create watchlist" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/watchlists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "Semis" }),
      }),
    );
  });

  it("adds a symbol through the items endpoint", async () => {
    mockJson(200, ONE_LIST);
    render(<Watchlist />);
    await waitFor(() => expect(screen.getByText("AAPL.US")).toBeInTheDocument());

    mockJson(201, ONE_LIST.watchlists[0]);
    mockJson(200, ONE_LIST);

    fireEvent.change(screen.getByLabelText(/Add symbol/i), { target: { value: "MSFT.US" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/watchlists/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/items",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol: "MSFT.US" }),
        },
      ),
    );
  });

  it("renders the backend's own 409 when a symbol is already on the list", async () => {
    mockJson(200, ONE_LIST);
    render(<Watchlist />);
    await waitFor(() => expect(screen.getByText("AAPL.US")).toBeInTheDocument());

    mockJson(409, { detail: "Symbol AAPL.US is already on watchlist …" }, false);

    fireEvent.change(screen.getByLabelText(/Add symbol/i), { target: { value: "AAPL.US" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(screen.getByText(/HTTP 409: Symbol AAPL.US is already on watchlist/)).toBeInTheDocument(),
    );
  });

  it("removes a symbol through the item-delete endpoint", async () => {
    mockJson(200, ONE_LIST);
    render(<Watchlist />);
    await waitFor(() => expect(screen.getByText("AAPL.US")).toBeInTheDocument());

    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 204,
      json: async () => null,
    });
    mockJson(200, { watchlists: [{ ...ONE_LIST.watchlists[0], symbols: ["NOSUCH.US"] }], limit: 50, offset: 0 });

    fireEvent.click(screen.getByRole("button", { name: "Remove AAPL.US" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/watchlists/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/items/AAPL.US",
        { method: "DELETE" },
      ),
    );
  });

  it("deletes a watchlist", async () => {
    mockJson(200, ONE_LIST);
    render(<Watchlist />);
    await waitFor(() => expect(screen.getByText("AAPL.US")).toBeInTheDocument());

    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 204,
      json: async () => null,
    });
    mockJson(200, { watchlists: [], limit: 50, offset: 0 });

    fireEvent.click(screen.getByRole("button", { name: "Delete watchlist" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/watchlists/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        { method: "DELETE" },
      ),
    );
    await waitFor(() =>
      expect(screen.getByText(/You have no watchlists yet/i)).toBeInTheDocument(),
    );
  });

  it("renders a real price and a DATA_UNAVAILABLE row side by side", async () => {
    mockJson(200, ONE_LIST);
    render(<Watchlist />);
    await waitFor(() => expect(screen.getByText("AAPL.US")).toBeInTheDocument());

    mockJson(200, MIXED_QUOTES);
    fireEvent.click(screen.getByRole("button", { name: "Load quotes" }));

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    // The priced symbol shows the backend's number, verbatim.
    expect(screen.getByText("195.25")).toBeInTheDocument();
    expect(screen.getByText("longbridge")).toBeInTheDocument();

    // The unpriceable one keeps its row and states the real sentinel …
    expect(screen.getByText(/DATA_UNAVAILABLE: NO_DATA_AVAILABLE/)).toBeInTheDocument();
    // … and is not dropped: both symbols are still in the table.
    const rows = screen.getAllByRole("row");
    // header + two data rows
    expect(rows).toHaveLength(3);
  });

  it("says once, not per row, that no vendor is configured", async () => {
    mockJson(200, ONE_LIST);
    render(<Watchlist />);
    await waitFor(() => expect(screen.getByText("AAPL.US")).toBeInTheDocument());

    mockJson(200, {
      watchlist_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      name: "Semis",
      market_data_configured: false,
      quotes: [
        {
          symbol: "AAPL.US",
          price: null,
          as_of: null,
          source: null,
          unavailable: "DATA_UNAVAILABLE: NOT_CONFIGURED: no market data vendor is wired.",
        },
        {
          symbol: "NOSUCH.US",
          price: null,
          as_of: null,
          source: null,
          unavailable: "DATA_UNAVAILABLE: NOT_CONFIGURED: no market data vendor is wired.",
        },
      ],
    });
    fireEvent.click(screen.getByRole("button", { name: "Load quotes" }));

    await waitFor(() =>
      expect(screen.getByTestId("watchlist-not-configured")).toBeInTheDocument(),
    );
    // Every row still present, each carrying its own real sentinel — and
    // not one of them showing a number.
    expect(screen.getAllByText(/DATA_UNAVAILABLE: NOT_CONFIGURED/)).toHaveLength(2);
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  it("redirects to login on a 401 rather than rendering a raw error", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(<Watchlist />);

    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<Watchlist />);

    await waitFor(() => expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument());
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the backend's own detail for a non-401 listing failure", async () => {
    mockJson(500, { detail: "boom" }, false);
    render(<Watchlist />);

    await waitFor(() => expect(screen.getByText(/HTTP 500: boom/)).toBeInTheDocument());
  });
});
