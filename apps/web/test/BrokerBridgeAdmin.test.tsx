import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import {
  BrokerCatalogue,
  BrokerCredentialsAdmin,
} from "@/components/admin/BrokerBridgeAdmin";

/**
 * The broker bridge's UI rules (Phase 90, D109).
 *
 * The important assertions are negative ones. A credential panel is only
 * as good as its worst render, so what is tested is that a secret never
 * reaches the screen — not even as a placeholder or a stored value — and
 * that a venue this build cannot reach never looks like one it can.
 */

const PROVIDERS = {
  note: "`adapter_status: catalogued` means this platform has no adapter for it yet.",
  providers: [
    {
      provider: "kraken",
      display_name: "Kraken",
      asset_classes: ["crypto"],
      order_types: ["limit", "market"],
      supports_short: false,
      supports_extended_hours: true,
      supports_fractional: true,
      supports_cancel: true,
      adapter_status: "catalogued",
      credential_fields: [
        { name: "api_key", label: "API key", secret: false, required: true, help: "id" },
        { name: "api_secret", label: "Private key", secret: true, required: true, help: "secret" },
      ],
      notes: "Spot crypto, 24/7.",
    },
  ],
};

const BROKERS = {
  brokers: [{ id: "b-1", name: "Kraken live", kind: "live", provider: "kraken" }],
};

const STATUS = {
  broker_id: "b-1",
  provider: "kraken",
  configured: true,
  updated_at: "2026-09-23T10:00:00Z",
  key_matches: true,
  unreadable_reason: null,
  fields: [
    {
      name: "api_key",
      label: "API key",
      secret: false,
      required: true,
      help: "id",
      present: true,
      value: "PUBLIC-ACCOUNT-ID",
    },
    {
      name: "api_secret",
      label: "Private key",
      secret: true,
      required: true,
      help: "secret",
      present: true,
      value: null,
    },
  ],
};

function wire(handlers: Record<string, { status?: number; body: unknown }>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const key = Object.keys(handlers).find((k) => url.includes(k));
      if (!key) return new Response(JSON.stringify({ detail: "no stub" }), { status: 404 });
      const { status = 200, body } = handlers[key];
      return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("BrokerCatalogue", () => {
  it("marks a venue with no adapter as catalogued rather than implying it works", async () => {
    wire({ "/api/brokers/providers": { body: PROVIDERS } });
    render(<BrokerCatalogue />);
    await waitFor(() => expect(screen.getByText("Kraken")).toBeInTheDocument());
    expect(screen.getByText("catalogued")).toBeInTheDocument();
    expect(screen.queryByText("implemented")).toBeNull();
    // The capability row is the venue's, not a guess.
    expect(screen.getByText("crypto")).toBeInTheDocument();
  });
});

describe("BrokerCredentialsAdmin", () => {
  it("shows a secret as set without ever rendering its value", async () => {
    wire({
      "/api/brokers/b-1/credentials": { body: STATUS },
      "/api/brokers": { body: BROKERS },
    });
    render(<BrokerCredentialsAdmin />);

    await waitFor(() => expect(screen.getByText("configured")).toBeInTheDocument());

    const secret = screen.getByLabelText(/Private key/) as HTMLInputElement;
    // Empty, and typed as a password: a stored secret is not readable, so
    // there is nothing to put here and nothing to reveal.
    expect(secret.value).toBe("");
    expect(secret.type).toBe("password");
    expect(secret.placeholder).toContain("stored");

    // A NON-secret keeps its value, because an operator has to be able to
    // confirm which account is wired.
    const pub = screen.getByLabelText(/API key/) as HTMLInputElement;
    expect(pub.value).toBe("PUBLIC-ACCOUNT-ID");
    expect(pub.type).toBe("text");

    // Both fields report presence.
    expect(screen.getAllByText("set")).toHaveLength(2);
  });

  it("surfaces a key mismatch as something to act on", async () => {
    wire({
      "/api/brokers/b-1/credentials": {
        body: {
          ...STATUS,
          configured: false,
          key_matches: false,
          unreadable_reason: "Encrypted under a different key. Re-enter these credentials.",
        },
      },
      "/api/brokers": { body: BROKERS },
    });
    render(<BrokerCredentialsAdmin />);
    await waitFor(() =>
      expect(screen.getByText(/Re-enter these credentials/)).toBeInTheDocument(),
    );
    expect(screen.getByText("not configured")).toBeInTheDocument();
  });

  it("says so when a provider needs no credentials at all", async () => {
    wire({
      "/api/brokers/b-1/credentials": {
        body: { ...STATUS, provider: "paper", configured: true, fields: [] },
      },
      "/api/brokers": {
        body: { brokers: [{ id: "b-1", name: "Paper", kind: "paper", provider: "paper" }] },
      },
    });
    render(<BrokerCredentialsAdmin />);
    await waitFor(() =>
      expect(screen.getByText(/needs no credentials/)).toBeInTheDocument(),
    );
    // No save control for a form with nothing to save.
    expect(screen.queryByText("Save credentials")).toBeNull();
  });
});
