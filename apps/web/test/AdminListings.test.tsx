import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import {
  UsersList,
  RolesList,
  BrokerGrantsList,
} from "@/components/admin/AdminListings";
import { sessionNavigation } from "@/lib/session";

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

describe("UsersList", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the real rows the backend returned", async () => {
    mockJson(200, {
      users: [
        { id: "u-1", email: "a@example.com", is_active: true, role_id: "r-1" },
        { id: "u-2", email: "b@example.com", is_active: false, role_id: null },
      ],
      limit: 50,
      offset: 0,
    });

    render(<UsersList />);

    await waitFor(() => expect(screen.getByText("a@example.com")).toBeInTheDocument());
    expect(screen.getByText("u-2")).toBeInTheDocument();
    expect(screen.getByText("false")).toBeInTheDocument();
    // Unassigned role renders as an em dash, never an invented value.
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("requests the listing with the documented limit/offset params", async () => {
    mockJson(200, { users: [], limit: 50, offset: 0 });
    render(<UsersList />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(fetch).toHaveBeenCalledWith("/api/admin/users?limit=50&offset=0");
  });

  it("shows an empty page as empty, not as sample rows", async () => {
    mockJson(200, { users: [], limit: 50, offset: 0 });
    render(<UsersList />);
    await waitFor(() =>
      expect(screen.getByText(/No users on this page/i)).toBeInTheDocument(),
    );
  });

  it("renders the backend's real 403 for a non-admin caller", async () => {
    mockJson(403, { detail: "Missing required permission: admin:manage" }, false);
    render(<UsersList />);
    await waitFor(() =>
      expect(screen.getByText(/Missing required permission: admin:manage/)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<UsersList />);
    await waitFor(() => expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument());
  });

  it("redirects to login on a 401 rather than rendering a raw error", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(<UsersList />);
    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("RolesList", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders each role's real permission list", async () => {
    mockJson(200, {
      roles: [
        {
          id: "r-1",
          name: "trader",
          description: "can submit paper trades",
          permissions: ["trade:submit:paper", "portfolio:view"],
        },
        { id: "r-2", name: "empty", description: null, permissions: [] },
      ],
      limit: 50,
      offset: 0,
    });

    render(<RolesList />);

    await waitFor(() => expect(screen.getByText("trader")).toBeInTheDocument());
    expect(
      screen.getByText("trade:submit:paper, portfolio:view"),
    ).toBeInTheDocument();
    // A role with no permissions shows as none, not as a guessed default.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("renders a real 404 detail if the backend ever returns one", async () => {
    mockJson(404, { detail: "Not Found" }, false);
    render(<RolesList />);
    await waitFor(() => expect(screen.getByText(/HTTP 404/)).toBeInTheDocument());
  });
});

describe("BrokerGrantsList", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the grant id that the revoke form needs", async () => {
    mockJson(200, {
      grants: [{ id: "g-1", user_id: "u-1", broker_id: "b-1" }],
      limit: 50,
      offset: 0,
    });

    render(<BrokerGrantsList />);

    await waitFor(() => expect(screen.getByText("g-1")).toBeInTheDocument());
    expect(screen.getByText("u-1")).toBeInTheDocument();
    expect(screen.getByText("b-1")).toBeInTheDocument();
  });

  it("renders the backend's real 403 for a non-admin caller", async () => {
    mockJson(403, { detail: "Missing required permission: admin:manage" }, false);
    render(<BrokerGrantsList />);
    await waitFor(() =>
      expect(screen.getByText(/admin:manage/)).toBeInTheDocument(),
    );
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<BrokerGrantsList />);
    await waitFor(() => expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument());
  });
});
