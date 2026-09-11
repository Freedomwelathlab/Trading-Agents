import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { DeploymentList } from "@/components/DeploymentList";
import type { StrategyDeploymentResponse } from "@/components/CreateDeploymentForm";
import { sessionNavigation } from "@/lib/session";

function deployment(
  overrides: Partial<StrategyDeploymentResponse> = {},
): StrategyDeploymentResponse {
  return {
    id: "dep-1",
    strategy_version_id: "v-1",
    broker_id: "11111111-1111-1111-1111-111111111111",
    mode: "paper",
    status: "pending_approval",
    symbols: ["AAPL.US", "MSFT.US"],
    bar_interval: "1d",
    requested_by_user_id: "u-1",
    approved_by_user_id: null,
    approved_at: null,
    paused_reason: null,
    stopped_at: null,
    last_evaluated_at: null,
    created_at: "2026-09-10T00:00:00Z",
    updated_at: "2026-09-10T00:00:00Z",
    ...overrides,
  };
}

function mockOnce(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

function calls() {
  return (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
}

describe("DeploymentList", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders EmptyNote for an empty list and never fetches", () => {
    render(<DeploymentList deployments={[]} onChanged={vi.fn()} />);
    expect(screen.getByText(/no deployments yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId("deployment-row")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("shows Approve + Stop (not Pause/Resume) for a pending_approval deployment and calls the approve endpoint", async () => {
    mockOnce(200, deployment({ status: "active" }));
    const onChanged = vi.fn();
    render(
      <DeploymentList deployments={[deployment()]} onChanged={onChanged} />,
    );

    expect(screen.getByTestId("deployment-status-dep-1")).toHaveTextContent(
      "pending_approval",
    );
    expect(screen.getByTestId("approve-dep-1")).toBeInTheDocument();
    expect(screen.getByTestId("stop-dep-1")).toBeInTheDocument();
    expect(screen.queryByTestId("pause-dep-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("resume-dep-1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("approve-dep-1"));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(calls()[0][0]).toBe("/api/deployments/dep-1/approve");
    expect(calls()[0][1].method).toBe("POST");
  });

  it("shows Pause + Stop for an active deployment and calls the pause endpoint", async () => {
    mockOnce(200, deployment({ status: "paused" }));
    render(
      <DeploymentList
        deployments={[deployment({ status: "active" })]}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("approve-dep-1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("pause-dep-1"));
    await waitFor(() => expect(calls()[0][0]).toBe("/api/deployments/dep-1/pause"));
  });

  it("shows Resume + Stop for a paused deployment and calls the resume endpoint", async () => {
    mockOnce(200, deployment({ status: "active" }));
    render(
      <DeploymentList
        deployments={[deployment({ status: "paused" })]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("resume-dep-1"));
    await waitFor(() => expect(calls()[0][0]).toBe("/api/deployments/dep-1/resume"));
    expect(calls()[0][1].method).toBe("POST");
  });

  it("shows no lifecycle buttons for a stopped deployment", () => {
    render(
      <DeploymentList
        deployments={[deployment({ status: "stopped", stopped_at: "2026-09-10T01:00:00Z" })]}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("approve-dep-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("pause-dep-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("resume-dep-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stop-dep-1")).not.toBeInTheDocument();
  });

  it("renders the separate-permission hint on a 403 from Approve, not a generic error", async () => {
    mockOnce(403, { detail: "Missing permission strategy:approve_deployment" }, false);
    render(
      <DeploymentList deployments={[deployment()]} onChanged={vi.fn()} />,
    );
    fireEvent.click(screen.getByTestId("approve-dep-1"));

    await waitFor(() =>
      expect(
        screen.getByTestId("approve-permission-hint-dep-1"),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId("approve-permission-hint-dep-1"),
    ).toHaveTextContent("strategy:approve_deployment");
    expect(
      screen.queryByTestId("deployment-action-error-dep-1"),
    ).not.toBeInTheDocument();
  });

  it("renders a plain-string 409 detail verbatim on a failed action", async () => {
    mockOnce(
      409,
      { detail: "NOT_ACTIVE: deployment is paused; only an active deployment can be paused." },
      false,
    );
    render(
      <DeploymentList
        deployments={[deployment({ status: "active" })]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("pause-dep-1"));

    await waitFor(() =>
      expect(
        screen.getByTestId("deployment-action-error-dep-1"),
      ).toHaveTextContent(
        "NOT_ACTIVE: deployment is paused; only an active deployment can be paused.",
      ),
    );
  });

  it("redirects to login on a 401 from an action", async () => {
    const spy = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockOnce(401, { detail: "Not authenticated" }, false);
    render(<DeploymentList deployments={[deployment()]} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByTestId("approve-dep-1"));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("session-expired"));
  });

  it("loads runner cycles and signals on expansion and renders both tables", async () => {
    // runs, then signals (Promise.all, in order)
    mockOnce(200, {
      items: [
        {
          id: "run-1",
          deployment_id: "dep-1",
          status: "succeeded",
          started_at: "2026-09-10T00:00:00Z",
          completed_at: "2026-09-10T00:00:05Z",
          symbols_evaluated: 2,
          signals_actionable: 1,
          orders_submitted: 1,
          orders_filled: 1,
          error_detail: null,
          created_at: "2026-09-10T00:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });
    mockOnce(200, {
      items: [
        {
          id: "sig-1",
          deployment_run_id: "run-1",
          symbol: "AAPL.US",
          as_of_bar_date: "2026-09-09",
          latest_close: "108.000000",
          signal: "buy",
          insufficient_data: false,
          explanation: "close crossed above sma_20 -> BUY",
          created_at: "2026-09-10T00:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });

    render(
      <DeploymentList
        deployments={[deployment({ status: "active" })]}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId("deployment-toggle-dep-1"));

    await waitFor(() =>
      expect(screen.getByTestId("deployment-run-row")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("run-status-run-1")).toHaveTextContent("succeeded");
    expect(screen.getByTestId("run-filled-run-1")).toHaveTextContent("1");

    const sigRow = screen.getByTestId("deployment-signal-row");
    expect(within(sigRow).getByText("close crossed above sma_20 -> BUY")).toBeInTheDocument();
    expect(screen.getByTestId("deployment-signal-pill-AAPL.US")).toHaveTextContent(
      "buy",
    );

    expect(calls()[0][0]).toBe("/api/deployments/dep-1/runs?limit=50&offset=0");
    expect(calls()[1][0]).toBe("/api/deployments/dep-1/signals?limit=50&offset=0");
  });

  it("renders a live capital halt with its full error detail (Phase 69, D087)", async () => {
    // A capital circuit breaker fired on real money and paused the
    // deployment. The operator must be able to read WHY straight off the
    // run row, so the backend's detail is rendered verbatim.
    mockOnce(200, {
      items: [
        {
          id: "run-halt",
          deployment_id: "dep-1",
          status: "skipped_live_risk_halt",
          started_at: "2026-09-11T00:00:00Z",
          completed_at: "2026-09-11T00:00:01Z",
          symbols_evaluated: 0,
          signals_actionable: 0,
          orders_submitted: 0,
          orders_filled: 0,
          error_detail:
            "daily loss circuit breaker: net P&L -400 is at or beyond the limit -300. Deployment paused; open positions were NOT liquidated.",
          created_at: "2026-09-11T00:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });
    mockOnce(200, { items: [], limit: 50, offset: 0 });

    render(
      <DeploymentList
        deployments={[deployment({ status: "paused" })]}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId("deployment-toggle-dep-1"));

    await waitFor(() =>
      expect(screen.getByTestId("run-status-run-halt")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("run-status-run-halt")).toHaveTextContent(
      "skipped_live_risk_halt",
    );
    expect(
      screen.getByText(/open positions were NOT liquidated/),
    ).toBeInTheDocument();
  });

  it("loads drift checks independently on expanding the Drift checks section", async () => {
    mockOnce(200, {
      items: [
        {
          id: "drift-1",
          deployment_id: "dep-1",
          status: "drift_detected",
          actual_win_rate_pct: "20.0000",
          expected_win_rate_pct: "55.0000",
          win_rate_deviation_pct: "35.0000",
          num_round_trips: 12,
          action_taken: "paused",
          detail:
            "actual win rate 20.0000% deviates 35.0000pp from the reference backtest's 55.0000% (threshold 30pp)",
          created_at: "2026-09-11T12:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });

    render(
      <DeploymentList
        deployments={[deployment({ status: "active" })]}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId("deployment-drift-toggle-dep-1"));

    await waitFor(() =>
      expect(screen.getByTestId("drift-check-row")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("drift-status-drift-1")).toHaveTextContent(
      "drift_detected",
    );
    expect(screen.getByTestId("drift-action-drift-1")).toHaveTextContent(
      "paused",
    );
    expect(calls()[0][0]).toBe(
      "/api/deployments/dep-1/drift-checks?limit=50&offset=0",
    );
    // Opening the Drift checks section alone must not also trigger the
    // Runs/Signals Promise.all fetch — the two sections load independently.
    expect(calls().length).toBe(1);
  });

  it("shows the empty-state message when a deployment has no drift checks yet", async () => {
    mockOnce(200, { items: [], limit: 50, offset: 0 });
    render(
      <DeploymentList
        deployments={[deployment({ status: "active" })]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("deployment-drift-toggle-dep-1"));
    await waitFor(() =>
      expect(screen.getByText(/no drift checks yet/i)).toBeInTheDocument(),
    );
  });
});
