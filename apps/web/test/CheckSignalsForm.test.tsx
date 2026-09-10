import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  CheckSignalsForm,
  parseSymbols,
  formatDetail,
  type SignalEvaluationResponse,
} from "@/components/CheckSignalsForm";
import { sessionNavigation } from "@/lib/session";

function evaluation(
  overrides: Partial<SignalEvaluationResponse> = {},
): SignalEvaluationResponse {
  return {
    id: `eval-${overrides.symbol ?? "x"}`,
    strategy_version_id: "v-1",
    symbol: "AAPL.US",
    bar_interval: "1d",
    as_of_bar_date: "2026-09-09",
    latest_close: "108.00",
    signal: "buy",
    entry_rule_held: true,
    exit_rule_held: false,
    insufficient_data: false,
    indicator_values: { sma_20: "105.50" },
    explanation:
      "close (108.00) crossed above sma_20 (105.50) on 2026-09-09 -> BUY",
    created_at: "2026-09-09T00:00:00Z",
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
  fireEvent.click(screen.getByRole("button", { name: /check signals/i }));
}

function sentBody() {
  const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
  return { url: call[0], method: call[1].method, body: JSON.parse(call[1].body) };
}

describe("CheckSignalsForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("parses a newline- and comma-separated symbol list into the right POST body", async () => {
    mockJson(201, { items: [evaluation()] });
    render(
      <CheckSignalsForm strategyId="s-1" versionId="v-1" onEvaluated={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbols$/i), {
      target: { value: "AAPL.US\nMSFT.US, GOOG.US\n\n  NVDA.US  " },
    });
    submit();

    await waitFor(() =>
      expect(screen.getByTestId("signals-result")).toBeInTheDocument(),
    );

    expect(sentBody()).toEqual({
      url: "/api/strategies/s-1/versions/v-1/signals",
      method: "POST",
      body: {
        symbols: ["AAPL.US", "MSFT.US", "GOOG.US", "NVDA.US"],
        bar_interval: "1d",
      },
    });
  });

  it("calls onEvaluated with the batch items and shows a confirmation count on a 201", async () => {
    const items = [
      evaluation({ symbol: "AAPL.US" }),
      evaluation({ symbol: "MSFT.US", signal: "hold" }),
    ];
    mockJson(201, { items });
    const onEvaluated = vi.fn();
    render(
      <CheckSignalsForm strategyId="s-1" versionId="v-1" onEvaluated={onEvaluated} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbols$/i), {
      target: { value: "AAPL.US, MSFT.US" },
    });
    submit();

    await waitFor(() => expect(onEvaluated).toHaveBeenCalledWith(items));
    expect(screen.getByTestId("signals-result")).toHaveTextContent(
      /evaluated 2 symbols/i,
    );
  });

  it("renders the plain-string 409 VERSION_NOT_VALIDATED detail verbatim", async () => {
    mockJson(
      409,
      { detail: "VERSION_NOT_VALIDATED: this strategy version has not been validated." },
      false,
    );
    render(
      <CheckSignalsForm strategyId="s-1" versionId="v-1" onEvaluated={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbols$/i), {
      target: { value: "AAPL.US" },
    });
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

  it("shows a validation message and does not POST for an empty textarea", () => {
    render(
      <CheckSignalsForm strategyId="s-1" versionId="v-1" onEvaluated={vi.fn()} />,
    );
    submit();

    expect(screen.getByTestId("signals-validation")).toHaveTextContent(
      /at least one symbol/i,
    );
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId("signals-result")).not.toBeInTheDocument();
  });

  it("shows a validation message and does not POST when only blanks are entered", () => {
    render(
      <CheckSignalsForm strategyId="s-1" versionId="v-1" onEvaluated={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbols$/i), {
      target: { value: "  \n , \n" },
    });
    submit();

    expect(screen.getByTestId("signals-validation")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("redirects to login on a 401 instead of showing a dead form", async () => {
    const spy = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(
      <CheckSignalsForm strategyId="s-1" versionId="v-1" onEvaluated={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText(/^symbols$/i), {
      target: { value: "AAPL.US" },
    });
    submit();

    await waitFor(() => expect(spy).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByTestId("signals-result")).not.toBeInTheDocument();
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
  it("passes a plain string sentinel through untouched", () => {
    expect(formatDetail("VERSION_NOT_VALIDATED: nope.")).toBe(
      "VERSION_NOT_VALIDATED: nope.",
    );
  });

  it("flattens a FastAPI request-shape validation error list", () => {
    expect(
      formatDetail([{ loc: ["body", "symbols"], msg: "field required" }]),
    ).toBe("body.symbols: field required");
  });
});
