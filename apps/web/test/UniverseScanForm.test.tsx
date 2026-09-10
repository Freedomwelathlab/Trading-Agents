import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  UniverseScanForm,
  parseSymbols,
  formatDetail,
  type UniverseScanDetailResponse,
} from "@/components/UniverseScanForm";
import { sessionNavigation } from "@/lib/session";

function scanDetail(
  overrides: Partial<UniverseScanDetailResponse> = {},
): UniverseScanDetailResponse {
  return {
    id: "scan-1",
    strategy_version_id: "v-1",
    bar_interval: "1d",
    start_date: "2026-01-01",
    end_date: "2026-06-30",
    starting_cash: "100000",
    scan_mode: "explicit_list",
    requested_symbols: ["AAPL.US", "MSFT.US"],
    status: "running",
    num_symbols: 2,
    num_succeeded: null,
    num_qualified: null,
    error_detail: null,
    created_at: "2026-09-01T00:00:00Z",
    completed_at: null,
    results: [],
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

function setDates() {
  fireEvent.change(screen.getByLabelText(/start date/i), {
    target: { value: "2026-01-01" },
  });
  fireEvent.change(screen.getByLabelText(/end date/i), {
    target: { value: "2026-06-30" },
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: /run scan/i }));
}

function sentBody() {
  const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
  return { url: call[0], method: call[1].method, body: JSON.parse(call[1].body) };
}

describe("UniverseScanForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("parses a newline-separated symbol list into the right POST body", async () => {
    mockJson(201, scanDetail());
    render(
      <UniverseScanForm strategyId="s-1" versionId="v-1" onScanComplete={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbols$/i), {
      target: { value: "AAPL.US\nMSFT.US\n\n  NVDA.US  " },
    });
    fireEvent.change(screen.getByLabelText(/starting cash/i), {
      target: { value: "50000" },
    });
    setDates();
    submit();

    await waitFor(() => expect(screen.getByTestId("scan-result")).toBeInTheDocument());

    expect(sentBody()).toEqual({
      url: "/api/strategies/s-1/versions/v-1/universe-scans",
      method: "POST",
      body: {
        symbols: ["AAPL.US", "MSFT.US", "NVDA.US"],
        bar_interval: "1d",
        start_date: "2026-01-01",
        end_date: "2026-06-30",
        starting_cash: "50000",
      },
    });
  });

  it("parses a comma-separated symbol list into the right POST body", async () => {
    mockJson(201, scanDetail());
    render(
      <UniverseScanForm strategyId="s-1" versionId="v-1" onScanComplete={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbols$/i), {
      target: { value: "AAPL.US, MSFT.US ,GOOG.US" },
    });
    setDates();
    submit();

    await waitFor(() => expect(screen.getByTestId("scan-result")).toBeInTheDocument());
    expect(sentBody().body.symbols).toEqual(["AAPL.US", "MSFT.US", "GOOG.US"]);
  });

  it("sends symbols: null when the textarea is empty and 'scan all ingested' is ticked", async () => {
    mockJson(201, scanDetail({ scan_mode: "all_ingested", requested_symbols: null }));
    render(
      <UniverseScanForm strategyId="s-1" versionId="v-1" onScanComplete={vi.fn()} />,
    );
    fireEvent.click(screen.getByLabelText(/scan all ingested symbols/i));
    setDates();
    submit();

    await waitFor(() => expect(screen.getByTestId("scan-result")).toBeInTheDocument());
    expect(sentBody().body.symbols).toBeNull();
    expect(screen.getByLabelText(/^symbols$/i)).toBeDisabled();
  });

  it("renders the plain-string 422 detail verbatim for a symbols: [] response", async () => {
    mockJson(
      422,
      {
        detail:
          "pass symbols to scan, or omit the field entirely to scan all ingested symbols",
      },
      false,
    );
    render(
      <UniverseScanForm strategyId="s-1" versionId="v-1" onScanComplete={vi.fn()} />,
    );
    setDates();
    submit();

    await waitFor(() =>
      expect(
        screen.getByText(
          /pass symbols to scan, or omit the field entirely to scan all ingested symbols/,
        ),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 422");
    expect(screen.queryByTestId("scan-result")).not.toBeInTheDocument();
  });

  it("renders the plain-string 409 VERSION_NOT_VALIDATED detail verbatim", async () => {
    mockJson(
      409,
      { detail: "VERSION_NOT_VALIDATED: this strategy version has not been validated." },
      false,
    );
    render(
      <UniverseScanForm strategyId="s-1" versionId="v-1" onScanComplete={vi.fn()} />,
    );
    setDates();
    submit();

    await waitFor(() =>
      expect(
        screen.getByText(
          /VERSION_NOT_VALIDATED: this strategy version has not been validated\./,
        ),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 409");
  });

  it("calls onScanComplete with the persisted scan on a 201", async () => {
    const scan = scanDetail();
    mockJson(201, scan);
    const onScanComplete = vi.fn();
    render(
      <UniverseScanForm strategyId="s-1" versionId="v-1" onScanComplete={onScanComplete} />,
    );
    setDates();
    submit();

    await waitFor(() =>
      expect(screen.getByTestId("scan-status-pill")).toHaveTextContent("running"),
    );
    expect(onScanComplete).toHaveBeenCalledWith(scan);
  });

  it("redirects to login on a 401 instead of showing a dead form", async () => {
    const spy = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(
      <UniverseScanForm strategyId="s-1" versionId="v-1" onScanComplete={vi.fn()} />,
    );
    setDates();
    submit();

    await waitFor(() => expect(spy).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByTestId("scan-result")).not.toBeInTheDocument();
  });
});

describe("parseSymbols", () => {
  it("splits on newlines and commas, trims, and drops blanks", () => {
    expect(parseSymbols("AAPL.US\nMSFT.US, GOOG.US\n\n  ")).toEqual([
      "AAPL.US",
      "MSFT.US",
      "GOOG.US",
    ]);
  });

  it("returns an empty array for whitespace-only input", () => {
    expect(parseSymbols("  \n , \n")).toEqual([]);
  });
});

describe("formatDetail", () => {
  it("passes a plain string sentinel through untouched", () => {
    expect(formatDetail("VERSION_NOT_VALIDATED: nope.")).toBe(
      "VERSION_NOT_VALIDATED: nope.",
    );
  });

  it("returns null for a missing detail", () => {
    expect(formatDetail(undefined)).toBeNull();
    expect(formatDetail(null)).toBeNull();
  });
});
