import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { DeploymentDriftTable } from "@/components/DeploymentDriftTable";
import type { DriftCheckResponse } from "@/components/CreateDeploymentForm";

function driftCheck(
  overrides: Partial<DriftCheckResponse> = {},
): DriftCheckResponse {
  return {
    id: "drift-1",
    deployment_id: "dep-1",
    status: "drift_detected",
    actual_win_rate_pct: "20.0000",
    expected_win_rate_pct: "55.0000",
    win_rate_deviation_pct: "35.0000",
    num_round_trips: 12,
    action_taken: "observed_only",
    detail:
      "actual win rate 20.0000% deviates 35.0000pp from the reference backtest's 55.0000% (threshold 30pp)",
    created_at: "2026-09-11T12:00:00Z",
    ...overrides,
  };
}

describe("DeploymentDriftTable", () => {
  it("shows a plain message and no table shell when driftChecks is empty", () => {
    render(<DeploymentDriftTable driftChecks={[]} />);
    expect(screen.getByText(/no drift checks yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId("drift-check-row")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders a drift_detected row with real numbers and the full detail text", () => {
    render(<DeploymentDriftTable driftChecks={[driftCheck()]} />);

    const row = screen.getByTestId("drift-check-row");
    expect(row).toBeInTheDocument();
    expect(screen.getByTestId("drift-status-drift-1")).toHaveTextContent(
      "drift_detected",
    );
    expect(screen.getByTestId("drift-actual-win-rate-drift-1")).toHaveTextContent(
      "20.0000%",
    );
    expect(screen.getByTestId("drift-expected-win-rate-drift-1")).toHaveTextContent(
      "55.0000%",
    );
    expect(screen.getByTestId("drift-deviation-drift-1")).toHaveTextContent(
      "35.0000%",
    );
    expect(screen.getByTestId("drift-round-trips-drift-1")).toHaveTextContent(
      "12",
    );
    expect(screen.getByTestId("drift-action-drift-1")).toHaveTextContent(
      "observed_only",
    );
    expect(screen.getByTestId("drift-detail-drift-1")).toHaveTextContent(
      "actual win rate 20.0000% deviates 35.0000pp from the reference backtest's 55.0000% (threshold 30pp)",
    );
  });

  it("renders a no_drift row correctly", () => {
    render(
      <DeploymentDriftTable
        driftChecks={[
          driftCheck({
            id: "drift-2",
            status: "no_drift",
            actual_win_rate_pct: "58.0000",
            expected_win_rate_pct: "55.0000",
            win_rate_deviation_pct: "3.0000",
            action_taken: "none",
            detail:
              "actual win rate 58.0000% is within 3.0000pp of the reference backtest's 55.0000% (threshold 30pp)",
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("drift-status-drift-2")).toHaveTextContent(
      "no_drift",
    );
    expect(screen.getByTestId("drift-action-drift-2")).toHaveTextContent(
      "none",
    );
  });

  it("shows em-dashes for all three win-rate fields on an insufficient_data row, never 0 or blank, and still shows detail", () => {
    render(
      <DeploymentDriftTable
        driftChecks={[
          driftCheck({
            id: "drift-3",
            status: "insufficient_data",
            actual_win_rate_pct: null,
            expected_win_rate_pct: null,
            win_rate_deviation_pct: null,
            num_round_trips: 3,
            action_taken: "none",
            detail: "only 3 of 10 required round trips closed so far",
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("drift-status-drift-3")).toHaveTextContent(
      "insufficient_data",
    );
    expect(screen.getByTestId("drift-actual-win-rate-drift-3")).toHaveTextContent(
      "—",
    );
    expect(screen.getByTestId("drift-actual-win-rate-drift-3")).not.toHaveTextContent(
      "0%",
    );
    expect(screen.getByTestId("drift-expected-win-rate-drift-3")).toHaveTextContent(
      "—",
    );
    expect(screen.getByTestId("drift-deviation-drift-3")).toHaveTextContent(
      "—",
    );
    expect(screen.getByTestId("drift-detail-drift-3")).toHaveTextContent(
      "only 3 of 10 required round trips closed so far",
    );
  });

  it("renders a paused action_taken row distinctly from none/observed_only", () => {
    render(
      <DeploymentDriftTable
        driftChecks={[
          driftCheck({ id: "drift-none", action_taken: "none" }),
          driftCheck({ id: "drift-observed", action_taken: "observed_only" }),
          driftCheck({ id: "drift-paused", action_taken: "paused" }),
        ]}
      />,
    );

    const noneTone = screen.getByTestId("drift-action-drift-none").className;
    const observedTone = screen.getByTestId(
      "drift-action-drift-observed",
    ).className;
    const pausedTone = screen.getByTestId("drift-action-drift-paused").className;

    expect(screen.getByTestId("drift-action-drift-paused")).toHaveTextContent(
      "paused",
    );
    // none/observed_only share the same (neutral) visual treatment...
    expect(noneTone).toBe(observedTone);
    // ...while paused is visually distinct from both.
    expect(pausedTone).not.toBe(noneTone);
    expect(pausedTone).not.toBe(observedTone);
  });
});
