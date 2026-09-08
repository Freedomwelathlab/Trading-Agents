import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { RunBacktestForm, formatDetail, type BacktestRunSummary } from "@/components/RunBacktestForm";
import { sessionNavigation } from "@/lib/session";

function succeededRun(overrides: Partial<BacktestRunSummary> = {}): BacktestRunSummary {
  return {
    id: "run-1",
    strategy_version_id: "v-1",
    symbol: "AAPL.US",
    bar_interval: "1d",
    start_date: "2026-01-01",
    end_date: "2026-06-30",
    starting_cash: "100000",
    status: "succeeded",
    final_equity: "112450.00",
    total_return_pct: "12.45",
    max_drawdown_pct: "6.10",
    win_rate_pct: "60.00",
    num_trades: 5,
    error_detail: null,
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:01:00Z",
    ...overrides,
  };
}

function failedRun(overrides: Partial<BacktestRunSummary> = {}): BacktestRunSummary {
  return {
    id: "run-2",
    strategy_version_id: "v-1",
    symbol: "AAPL.US",
    bar_interval: "1d",
    start_date: "2026-01-01",
    end_date: "2026-06-30",
    starting_cash: "100000",
    status: "failed",
    final_equity: null,
    total_return_pct: null,
    max_drawdown_pct: null,
    win_rate_pct: null,
    num_trades: null,
    error_detail: "DATA_UNAVAILABLE: vendor returned no closes for the window.",
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:01:00Z",
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

function run() {
  fireEvent.click(screen.getByRole("button", { name: /run backtest/i }));
}

describe("RunBacktestForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("posts the real request shape, including the hardcoded bar_interval, to the trigger route", async () => {
    mockJson(201, succeededRun());
    const onRunComplete = vi.fn();

    render(
      <RunBacktestForm strategyId="s-1" versionId="v-1" onRunComplete={onRunComplete} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbol$/i), { target: { value: "MSFT.US" } });
    fireEvent.change(screen.getByLabelText(/starting cash/i), { target: { value: "50000" } });
    fireEvent.change(screen.getByLabelText(/start date/i), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/end date/i), { target: { value: "2026-06-30" } });
    run();

    await waitFor(() => expect(screen.getByTestId("run-result")).toBeInTheDocument());

    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[0]).toBe("/api/strategies/s-1/versions/v-1/backtests");
    expect(call[1].method).toBe("POST");
    const sent = JSON.parse(call[1].body);
    expect(sent).toEqual({
      symbol: "MSFT.US",
      bar_interval: "1d",
      start_date: "2026-01-01",
      end_date: "2026-06-30",
      starting_cash: "50000",
    });
  });

  it("shows a positive result summary and calls onRunComplete for a succeeded run", async () => {
    const okRun = succeededRun({ total_return_pct: "12.45" });
    mockJson(201, okRun);
    const onRunComplete = vi.fn();

    render(
      <RunBacktestForm strategyId="s-1" versionId="v-1" onRunComplete={onRunComplete} />,
    );
    run();

    await waitFor(() =>
      expect(screen.getByTestId("run-status-pill")).toHaveTextContent("succeeded"),
    );
    expect(screen.getByTestId("run-total-return")).toHaveTextContent("12.45");
    expect(onRunComplete).toHaveBeenCalledWith(okRun);
    expect(screen.queryByTestId("run-error-detail")).not.toBeInTheDocument();
  });

  it("shows the real error detail — not a false success — for a 201 response with status: failed", async () => {
    const badRun = failedRun();
    mockJson(201, badRun);
    const onRunComplete = vi.fn();

    render(
      <RunBacktestForm strategyId="s-1" versionId="v-1" onRunComplete={onRunComplete} />,
    );
    run();

    await waitFor(() =>
      expect(screen.getByTestId("run-status-pill")).toHaveTextContent("failed"),
    );
    expect(screen.getByTestId("run-error-detail")).toHaveTextContent(
      "DATA_UNAVAILABLE: vendor returned no closes for the window.",
    );
    // A 201 is still a real, persisted run — onRunComplete still fires so
    // the list above can show it — but nothing here claims success.
    expect(onRunComplete).toHaveBeenCalledWith(badRun);
    expect(screen.queryByTestId("run-total-return")).not.toBeInTheDocument();
  });

  it("renders the plain-string 409 VERSION_NOT_VALIDATED detail verbatim, not paraphrased", async () => {
    mockJson(
      409,
      { detail: "VERSION_NOT_VALIDATED: this strategy version has not been validated." },
      false,
    );

    render(<RunBacktestForm strategyId="s-1" versionId="v-1" onRunComplete={vi.fn()} />);
    run();

    await waitFor(() =>
      expect(
        screen.getByText(/VERSION_NOT_VALIDATED: this strategy version has not been validated\./),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 409");
    expect(screen.queryByTestId("run-result")).not.toBeInTheDocument();
  });

  it("renders a real 422 validation error legibly instead of [object Object]", async () => {
    mockJson(
      422,
      {
        detail: [
          { loc: ["body", "starting_cash"], msg: "Input should be greater than 0" },
        ],
      },
      false,
    );

    render(<RunBacktestForm strategyId="s-1" versionId="v-1" onRunComplete={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/starting cash/i), { target: { value: "0" } });
    run();

    await waitFor(() =>
      expect(
        screen.getByText(/body\.starting_cash: Input should be greater than 0/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });

  it("redirects to login on a 401 instead of showing a dead form", async () => {
    const spy = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);

    render(<RunBacktestForm strategyId="s-1" versionId="v-1" onRunComplete={vi.fn()} />);
    run();

    await waitFor(() => expect(spy).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByTestId("run-result")).not.toBeInTheDocument();
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));

    render(<RunBacktestForm strategyId="s-1" versionId="v-1" onRunComplete={vi.fn()} />);
    run();

    await waitFor(() =>
      expect(
        screen.getByText(/DATA_UNAVAILABLE: could not reach the trading API/),
      ).toBeInTheDocument(),
    );
  });
});

describe("formatDetail", () => {
  it("passes a plain string sentinel through untouched", () => {
    expect(formatDetail("VERSION_NOT_VALIDATED: not validated.")).toBe(
      "VERSION_NOT_VALIDATED: not validated.",
    );
  });

  it("returns null for a missing detail", () => {
    expect(formatDetail(undefined)).toBeNull();
    expect(formatDetail(null)).toBeNull();
  });
});
