import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import StrategyResearchAssistant from "@/components/StrategyResearchAssistant";

/**
 * Phase 67 / D085. Same conventions as `StrategyLeaderboard.test.tsx` /
 * `StrategyList.test.tsx`: this component (not its thin `AppShell`-wrapping
 * page — see `app/strategies/research/page.tsx`, mirroring
 * `app/strategies/leaderboard/page.tsx`) is rendered directly, `fetch` is
 * stubbed, and every error case asserts the backend's own `detail` text is
 * shown verbatim.
 *
 * The property under test throughout is that four distinct backend
 * outcomes stay visually and textually distinct: a valid draft, an
 * invalid-but-parsed draft, a calm NOT_CONFIGURED notice, and a real
 * failure never collapse into one generic "error" rendering.
 */

const VALID_RESPONSE = {
  name: "RSI Mean Reversion",
  definition: {
    indicators: [{ id: "rsi_14", type: "rsi", period: 14 }],
    entry_rule: { op: "lt", left: "rsi_14", right: "close" },
    exit_rule: { op: "gt", left: "rsi_14", right: "close" },
    position_sizing: { type: "fixed_fraction", fraction: "0.25" },
  },
  rationale: "This strategy buys when RSI suggests the market is oversold.",
  is_valid: true,
  validation_errors: [],
};

const INVALID_RESPONSE = {
  ...VALID_RESPONSE,
  is_valid: false,
  validation_errors: [
    "unknown top-level key 'stop_rule' - allowed keys are: indicators, entry_rule, exit_rule, position_sizing",
  ],
};

function mockJson(status: number, body: unknown) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400,
    status,
    json: async () => body,
  });
}

function submitBrief(value = "A mean-reversion strategy using RSI") {
  fireEvent.change(
    screen.getByLabelText(/describe the strategy idea you want to explore/i),
    { target: { value } },
  );
  fireEvent.click(screen.getByRole("button", { name: /propose a draft/i }));
}

describe("StrategyResearchAssistant", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows a live character count as the brief is typed", () => {
    render(<StrategyResearchAssistant />);
    fireEvent.change(
      screen.getByLabelText(/describe the strategy idea you want to explore/i),
      { target: { value: "RSI idea" } },
    );
    expect(screen.getByText("8/500 characters")).toBeInTheDocument();
  });

  it("blocks submission with an empty brief and never calls fetch", () => {
    render(<StrategyResearchAssistant />);
    fireEvent.click(screen.getByRole("button", { name: /propose a draft/i }));
    expect(screen.getByTestId("research-validation")).toHaveTextContent(
      /describe the strategy idea/i,
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("posts the trimmed brief to the proxy route", async () => {
    mockJson(200, VALID_RESPONSE);
    render(<StrategyResearchAssistant />);
    submitBrief("  A mean-reversion strategy using RSI  ");

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/strategies/research/propose");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      brief: "A mean-reversion strategy using RSI",
    });
  });

  it("renders a valid draft's name, rationale and definition, with no structural-problems callout", async () => {
    mockJson(200, VALID_RESPONSE);
    render(<StrategyResearchAssistant />);
    submitBrief();

    const result = await screen.findByTestId("research-result");
    expect(result).toHaveTextContent("RSI Mean Reversion");
    expect(result).toHaveTextContent(/buys when RSI suggests the market is oversold/i);

    const definition = screen.getByTestId("research-definition");
    expect(definition).toHaveTextContent('"type": "rsi"');
    expect(definition).toHaveTextContent('"fixed_fraction"');

    expect(screen.queryByTestId("research-invalid-callout")).toBeNull();
    expect(result).toHaveTextContent(/copy the indicators/i);
  });

  it("renders an invalid draft with a visible, distinct structural-problems callout", async () => {
    mockJson(200, INVALID_RESPONSE);
    render(<StrategyResearchAssistant />);
    submitBrief();

    const result = await screen.findByTestId("research-result");
    // The name/rationale/definition still render — an invalid draft is not hidden.
    expect(result).toHaveTextContent("RSI Mean Reversion");
    expect(screen.getByTestId("research-definition")).toHaveTextContent('"type": "rsi"');

    const callout = screen.getByTestId("research-invalid-callout");
    expect(callout).toHaveTextContent("This draft has structural problems:");
    expect(callout).toHaveTextContent(
      "unknown top-level key 'stop_rule' - allowed keys are: indicators, entry_rule, exit_rule, position_sizing",
    );
    // Never phrased as though it were the valid case's save-it note.
    expect(result).not.toHaveTextContent(/copy the indicators/i);
  });

  it("renders a calm, non-error notice for a 400 NOT_CONFIGURED response", async () => {
    mockJson(400, {
      detail: "NOT_CONFIGURED: no LLM provider is wired (see docs/DECISIONS.md D018).",
    });
    render(<StrategyResearchAssistant />);
    submitBrief();

    const notice = await screen.findByTestId("research-not-configured");
    expect(notice).toHaveTextContent(/NOT_CONFIGURED: no LLM provider is wired/);
    expect(notice).toHaveAttribute("role", "status");
    expect(screen.queryByTestId("research-failed")).toBeNull();
    expect(screen.queryByTestId("research-result")).toBeNull();
  });

  it("renders a distinct failure message for a 502 from the LLM", async () => {
    mockJson(502, { detail: "LLM_OUTPUT_INVALID: response was not valid JSON" });
    render(<StrategyResearchAssistant />);
    submitBrief();

    const failure = await screen.findByTestId("research-failed");
    expect(failure).toHaveTextContent(/try again/i);
    expect(failure).toHaveAttribute("role", "alert");
    expect(screen.queryByTestId("research-not-configured")).toBeNull();
  });

  it("renders a distinct failure message when the fetch itself throws", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));
    render(<StrategyResearchAssistant />);
    submitBrief();

    const failure = await screen.findByTestId("research-failed");
    expect(failure).toHaveTextContent(/could not reach the research assistant/i);
    expect(failure).toHaveTextContent(/try again/i);
  });
});
