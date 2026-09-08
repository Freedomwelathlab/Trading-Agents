import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import StrategyBuilderForm, {
  type StrategyVersionResponse,
} from "@/components/StrategyBuilderForm";

/**
 * Same conventions as the other component suites here: `fetch` is stubbed
 * and every assertion is about what the component rendered or sent from a
 * real backend shape.
 */

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

function draftVersion(overrides: Partial<StrategyVersionResponse> = {}): StrategyVersionResponse {
  return {
    id: "v1",
    strategy_id: "s1",
    version_number: 1,
    definition: {
      indicators: [{ id: "sma_20", type: "sma", period: 20 }],
      entry_rule: { op: "crosses_above", left: "sma_20", right: "close" },
      exit_rule: { op: "crosses_below", left: "sma_20", right: "close" },
      position_sizing: { type: "all_in" },
    },
    definition_hash: "hash1",
    status: "draft",
    created_by_user_id: "u1",
    created_at: "2026-09-01T00:00:00Z",
    validated_at: null,
    ...overrides,
  };
}

describe("StrategyBuilderForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the sample StrategyDefinition it was given", () => {
    render(<StrategyBuilderForm strategyId="s1" version={draftVersion()} />);

    const row0 = within(screen.getByTestId("indicator-row-0"));
    expect(row0.getByLabelText("ID")).toHaveValue("sma_20");
    expect(row0.getByLabelText("Type")).toHaveValue("sma");
    expect(row0.getByLabelText("Period")).toHaveValue(20);

    expect(screen.getByLabelText("Entry op")).toHaveValue("crosses_above");
    expect(screen.getByLabelText("Entry left")).toHaveValue("sma_20");
    expect(screen.getByLabelText("Entry right")).toHaveValue("close");
    expect(screen.getByLabelText("Exit op")).toHaveValue("crosses_below");
    expect(screen.getByLabelText("Sizing type")).toHaveValue("all_in");
  });

  it("adds an indicator row with sensible defaults", () => {
    render(<StrategyBuilderForm strategyId="s1" version={draftVersion()} />);

    fireEvent.click(screen.getByRole("button", { name: "+ Add indicator" }));

    expect(screen.getAllByTestId(/indicator-row-/)).toHaveLength(2);
    const row1 = within(screen.getByTestId("indicator-row-1"));
    expect(row1.getByLabelText("ID")).toHaveValue("ind_2");
    expect(row1.getByLabelText("Type")).toHaveValue("sma");
    expect(row1.getByLabelText("Period")).toHaveValue(20);
  });

  it("removes an indicator row", () => {
    render(<StrategyBuilderForm strategyId="s1" version={draftVersion()} />);

    fireEvent.click(screen.getByRole("button", { name: "+ Add indicator" }));
    expect(screen.getAllByTestId(/indicator-row-/)).toHaveLength(2);

    const row1 = within(screen.getByTestId("indicator-row-1"));
    fireEvent.click(row1.getByRole("button", { name: "Remove" }));

    expect(screen.getAllByTestId(/indicator-row-/)).toHaveLength(1);
  });

  it("PATCHes the full assembled definition on Save draft", async () => {
    mockJson(200, draftVersion());
    render(<StrategyBuilderForm strategyId="s1" version={draftVersion()} />);

    fireEvent.click(screen.getByRole("button", { name: "+ Add indicator" }));
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/strategies/s1/versions/v1",
      expect.objectContaining({ method: "PATCH" }),
    ));

    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    const sentBody = JSON.parse(call[1].body);
    expect(sentBody).toEqual({
      definition: {
        indicators: [
          { id: "sma_20", type: "sma", period: 20 },
          { id: "ind_2", type: "sma", period: 20 },
        ],
        entry_rule: { op: "crosses_above", left: "sma_20", right: "close" },
        exit_rule: { op: "crosses_below", left: "sma_20", right: "close" },
        position_sizing: { type: "all_in" },
      },
    });
    await waitFor(() => expect(screen.getByText("Saved.")).toBeInTheDocument());
  });

  it("renders every string in a 422 errors array as its own visible line", async () => {
    mockJson(
      422,
      {
        detail: {
          errors: [
            "entry_rule references unknown indicator 'rsi_9'",
            "position_sizing.fraction must be in (0, 1]",
          ],
        },
      },
      false,
    );
    render(<StrategyBuilderForm strategyId="s1" version={draftVersion()} />);

    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    await waitFor(() =>
      expect(
        screen.getByText("entry_rule references unknown indicator 'rsi_9'"),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText("position_sizing.fraction must be in (0, 1]"),
    ).toBeInTheDocument();
  });

  it("renders the plain-string detail from a 409 (version not a draft)", async () => {
    mockJson(409, { detail: "VERSION_NOT_DRAFT: version v1 is already validated" }, false);
    render(<StrategyBuilderForm strategyId="s1" version={draftVersion()} />);

    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    await waitFor(() =>
      expect(
        screen.getByText("VERSION_NOT_DRAFT: version v1 is already validated"),
      ).toBeInTheDocument(),
    );
  });

  it("disables every control when the version is not a draft", () => {
    render(
      <StrategyBuilderForm
        strategyId="s1"
        version={draftVersion({ status: "validated", validated_at: "2026-09-02T00:00:00Z" })}
      />,
    );

    expect(screen.getByText(/this version is validated/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "+ Add indicator" })).toBeDisabled();

    const row0 = within(screen.getByTestId("indicator-row-0"));
    expect(row0.getByLabelText("ID")).toBeDisabled();
    expect(row0.getByLabelText("Type")).toBeDisabled();
    expect(row0.getByLabelText("Period")).toBeDisabled();
    expect(row0.getByRole("button", { name: "Remove" })).toBeDisabled();
    expect(screen.getByLabelText("Entry op")).toBeDisabled();
    expect(screen.getByLabelText("Entry left")).toBeDisabled();
    expect(screen.getByLabelText("Sizing type")).toBeDisabled();
  });
});
