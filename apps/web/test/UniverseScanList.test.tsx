import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { UniverseScanList } from "@/components/UniverseScanList";
import type { UniverseScanSummary } from "@/components/UniverseScanForm";

function succeededScan(overrides: Partial<UniverseScanSummary> = {}): UniverseScanSummary {
  return {
    id: "scan-1",
    strategy_version_id: "v-1",
    bar_interval: "1d",
    start_date: "2026-01-01",
    end_date: "2026-06-30",
    starting_cash: "100000",
    scan_mode: "explicit_list",
    requested_symbols: ["AAPL.US", "MSFT.US", "NVDA.US"],
    status: "succeeded",
    num_symbols: 3,
    num_succeeded: 2,
    num_qualified: 1,
    error_detail: null,
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:05:00Z",
    ...overrides,
  };
}

function failedScan(overrides: Partial<UniverseScanSummary> = {}): UniverseScanSummary {
  return {
    id: "scan-2",
    strategy_version_id: "v-1",
    bar_interval: "1d",
    start_date: "2026-02-01",
    end_date: "2026-03-01",
    starting_cash: "100000",
    scan_mode: "all_ingested",
    requested_symbols: null,
    status: "failed",
    num_symbols: null,
    num_succeeded: null,
    num_qualified: null,
    error_detail: "DATA_UNAVAILABLE: nothing ingested for the window.",
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:01:00Z",
    ...overrides,
  };
}

describe("UniverseScanList", () => {
  it("renders mixed succeeded/failed rows with correct counts, modes, and em-dashes for failed", () => {
    const ok = succeededScan();
    const bad = failedScan();
    render(
      <UniverseScanList strategyId="s-1" versionId="v-1" scans={[ok, bad]} />,
    );

    expect(screen.getAllByTestId("universe-scan-row")).toHaveLength(2);

    expect(screen.getByTestId(`mode-${ok.id}`)).toHaveTextContent("3 symbols");
    expect(screen.getByTestId(`scanned-${ok.id}`)).toHaveTextContent("3");
    expect(screen.getByTestId(`succeeded-${ok.id}`)).toHaveTextContent("2");
    expect(screen.getByTestId(`qualified-${ok.id}`)).toHaveTextContent("1");

    expect(screen.getByTestId(`mode-${bad.id}`)).toHaveTextContent("all ingested");
    expect(screen.getByTestId(`scanned-${bad.id}`)).toHaveTextContent("—");
    expect(screen.getByTestId(`succeeded-${bad.id}`)).toHaveTextContent("—");
    expect(screen.getByTestId(`qualified-${bad.id}`)).toHaveTextContent("—");
    expect(screen.getByTestId(`scanned-${bad.id}`)).not.toHaveTextContent("0");
  });

  it("renders the EmptyNote, not an empty table shell, when there are no scans", () => {
    render(<UniverseScanList strategyId="s-1" versionId="v-1" scans={[]} />);
    expect(screen.getByText(/no universe scans yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("links each row's View action to the real scan detail path", () => {
    const ok = succeededScan({ id: "scan-abc" });
    render(<UniverseScanList strategyId="strat-42" versionId="v-1" scans={[ok]} />);

    const link = screen.getByRole("link", { name: /view/i });
    expect(link).toHaveAttribute(
      "href",
      "/strategies/strat-42/universe-scans/scan-abc",
    );
  });
});
