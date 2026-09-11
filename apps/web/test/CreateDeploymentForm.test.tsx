import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  CreateDeploymentForm,
  parseSymbols,
  formatDetail,
  type StrategyDeploymentResponse,
} from "@/components/CreateDeploymentForm";
import { sessionNavigation } from "@/lib/session";

const BROKER_ID = "11111111-1111-1111-1111-111111111111";

function deployment(
  overrides: Partial<StrategyDeploymentResponse> = {},
): StrategyDeploymentResponse {
  return {
    id: "dep-1",
    strategy_version_id: "v-1",
    broker_id: BROKER_ID,
    mode: "paper",
    status: "pending_approval",
    symbols: ["AAPL.US"],
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

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: /create deployment/i }));
}

function sentBody() {
  const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
  return { url: call[0], method: call[1].method, body: JSON.parse(call[1].body) };
}

function fillBroker(value = BROKER_ID) {
  fireEvent.change(screen.getByLabelText(/paper broker id/i), {
    target: { value },
  });
}

function fillSymbols(value: string) {
  fireEvent.change(screen.getByLabelText(/^symbols$/i), { target: { value } });
}

describe("CreateDeploymentForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("builds the right POST body and renders the deployment as pending_approval", async () => {
    mockJson(201, deployment());
    render(
      <CreateDeploymentForm strategyId="s-1" versionId="v-1" onCreated={vi.fn()} />,
    );
    fillBroker();
    fillSymbols("AAPL.US\nMSFT.US, GOOG.US\n\n  NVDA.US  ");
    submit();

    await waitFor(() =>
      expect(screen.getByTestId("deployment-created")).toBeInTheDocument(),
    );

    expect(sentBody()).toEqual({
      url: "/api/strategies/s-1/versions/v-1/deployments",
      method: "POST",
      body: {
        broker_id: BROKER_ID,
        symbols: ["AAPL.US", "MSFT.US", "GOOG.US", "NVDA.US"],
        bar_interval: "1d",
        mode: "paper",
      },
    });
    expect(screen.getByTestId("deployment-created")).toHaveTextContent(
      /pending_approval/i,
    );
    expect(screen.getByTestId("deployment-created")).toHaveTextContent(
      /not trading yet/i,
    );
  });

  it("calls onCreated with the created deployment on a 201", async () => {
    const created = deployment({ id: "dep-42" });
    mockJson(201, created);
    const onCreated = vi.fn();
    render(
      <CreateDeploymentForm strategyId="s-1" versionId="v-1" onCreated={onCreated} />,
    );
    fillBroker();
    fillSymbols("AAPL.US");
    submit();

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
  });

  it("renders a plain-string 409 guardrail detail verbatim", async () => {
    mockJson(
      409,
      { detail: "NOT_A_PAPER_BROKER: broker … is kind 'live'." },
      false,
    );
    render(
      <CreateDeploymentForm strategyId="s-1" versionId="v-1" onCreated={vi.fn()} />,
    );
    fillBroker();
    fillSymbols("AAPL.US");
    submit();

    await waitFor(() =>
      expect(
        screen.getByText(/NOT_A_PAPER_BROKER: broker … is kind 'live'\./),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 409");
  });

  it("does not POST for an empty broker id", () => {
    render(
      <CreateDeploymentForm strategyId="s-1" versionId="v-1" onCreated={vi.fn()} />,
    );
    fillSymbols("AAPL.US");
    submit();

    expect(screen.getByTestId("deployment-validation")).toHaveTextContent(
      /broker/i,
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not POST for a broker id that is not a UUID", () => {
    render(
      <CreateDeploymentForm strategyId="s-1" versionId="v-1" onCreated={vi.fn()} />,
    );
    fillBroker("not-a-uuid");
    fillSymbols("AAPL.US");
    submit();

    expect(screen.getByTestId("deployment-validation")).toHaveTextContent(
      /must be a uuid/i,
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not POST for an empty symbol list", () => {
    render(
      <CreateDeploymentForm strategyId="s-1" versionId="v-1" onCreated={vi.fn()} />,
    );
    fillBroker();
    fillSymbols("  \n , \n");
    submit();

    expect(screen.getByTestId("deployment-validation")).toHaveTextContent(
      /at least one symbol/i,
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("redirects to login on a 401 instead of showing a dead form", async () => {
    const spy = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(
      <CreateDeploymentForm strategyId="s-1" versionId="v-1" onCreated={vi.fn()} />,
    );
    fillBroker();
    fillSymbols("AAPL.US");
    submit();

    await waitFor(() => expect(spy).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByTestId("deployment-created")).not.toBeInTheDocument();
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
});

describe("formatDetail", () => {
  it("passes a plain string 409 sentinel through untouched", () => {
    expect(formatDetail("TOO_MANY_SYMBOLS: 51 symbols, limit is 50.")).toBe(
      "TOO_MANY_SYMBOLS: 51 symbols, limit is 50.",
    );
  });

  it("flattens a FastAPI request-shape validation error list", () => {
    expect(
      formatDetail([{ loc: ["body", "broker_id"], msg: "value is not a valid uuid" }]),
    ).toBe("body.broker_id: value is not a valid uuid");
  });
});
