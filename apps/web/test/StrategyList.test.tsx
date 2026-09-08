import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import StrategyList from "@/components/StrategyList";
import { sessionNavigation } from "@/lib/session";

/**
 * Same conventions as the other component suites here (see
 * `Watchlist.test.tsx`): `fetch` is stubbed, every assertion is about what
 * the component rendered from a real backend shape, and every error case
 * asserts the backend's own `detail` text is shown verbatim.
 */

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

const ONE_LIST = {
  items: [
    {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      name: "SMA Cross",
      description: "A simple moving-average crossover.",
      status: "active",
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-02T00:00:00Z",
      latest_version_number: 2,
      latest_version_status: "draft",
    },
  ],
  limit: 50,
  offset: 0,
};

describe("StrategyList", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads the caller's strategies on mount and renders them", async () => {
    mockJson(200, ONE_LIST);
    render(<StrategyList />);

    await waitFor(() => expect(screen.getByText("SMA Cross")).toBeInTheDocument());
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.getAllByText("draft").length).toBeGreaterThan(0);
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/api/strategies?limit=50&offset=0", {
      cache: "no-store",
    });

    const link = screen.getByRole("link", { name: "SMA Cross" });
    expect(link).toHaveAttribute(
      "href",
      "/strategies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    );
  });

  it("shows an explicit empty state rather than inventing a strategy", async () => {
    mockJson(200, { items: [], limit: 50, offset: 0 });
    render(<StrategyList />);

    await waitFor(() =>
      expect(screen.getByText(/You have no strategies yet/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the backend's own detail on a listing failure", async () => {
    mockJson(500, { detail: "boom" }, false);
    render(<StrategyList />);

    await waitFor(() => expect(screen.getByText(/HTTP 500: boom/)).toBeInTheDocument());
  });

  it("redirects to login on a 401 rather than rendering a raw error", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(<StrategyList />);

    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("creates a strategy with the name typed and reloads the list", async () => {
    mockJson(200, { items: [], limit: 50, offset: 0 });
    render(<StrategyList />);
    await waitFor(() =>
      expect(screen.getByText(/You have no strategies yet/i)).toBeInTheDocument(),
    );

    mockJson(201, {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      owner_user_id: "u1",
      name: "New Strat",
      description: null,
      status: "active",
      created_at: "2026-09-08T00:00:00Z",
      updated_at: "2026-09-08T00:00:00Z",
      latest_version: {
        id: "v1",
        strategy_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        version_number: 1,
        definition: {},
        definition_hash: "h",
        status: "draft",
        created_by_user_id: "u1",
        created_at: "2026-09-08T00:00:00Z",
        validated_at: null,
      },
    });
    mockJson(200, {
      items: [
        {
          id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          name: "New Strat",
          description: null,
          status: "active",
          created_at: "2026-09-08T00:00:00Z",
          updated_at: "2026-09-08T00:00:00Z",
          latest_version_number: 1,
          latest_version_status: "draft",
        },
      ],
      limit: 50,
      offset: 0,
    });

    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "New Strat" } });
    fireEvent.click(screen.getByRole("button", { name: "Create strategy" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/strategies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "New Strat" }),
      }),
    );
    await waitFor(() => expect(screen.getByText("New Strat")).toBeInTheDocument());
  });

  it("includes description in the create body only when the field is filled in", async () => {
    mockJson(200, { items: [], limit: 50, offset: 0 });
    render(<StrategyList />);
    await waitFor(() =>
      expect(screen.getByText(/You have no strategies yet/i)).toBeInTheDocument(),
    );

    mockJson(422, { detail: [{ loc: ["body", "name"], msg: "field required" }] }, false);

    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "X" } });
    fireEvent.change(screen.getByLabelText(/description/i), { target: { value: "notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Create strategy" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenLastCalledWith("/api/strategies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "X", description: "notes" }),
      }),
    );
    await waitFor(() =>
      expect(screen.getByText(/body\.name: field required/)).toBeInTheDocument(),
    );
  });
});
