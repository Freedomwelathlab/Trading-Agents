/**
 * Server-only helper for talking to the Trading OS API from Next.js route
 * handlers. Never imported from a client component — the backend base URL
 * and the auth cookie both stay server-side.
 */

import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

const DEFAULT_BASE_URL = "http://localhost:8000";

export function backendBaseUrl(): string {
  return process.env.API_BASE_URL ?? DEFAULT_BASE_URL;
}

export const AUTH_COOKIE_NAME = "trading_os_token";

/** Thin wrapper so route handlers don't repeat base-URL joining. */
export function backendUrl(path: string): string {
  return `${backendBaseUrl()}${path}`;
}

/**
 * GET with a JSON body — needed for `GET /brokers/{broker_id}/portfolio`
 * (D022's `marks` payload; see docs/API.md). Node's built-in `fetch`
 * (undici) throws "Request with GET/HEAD method cannot have body" for any
 * GET carrying a body, per the Fetch spec, so this bypasses `fetch`
 * entirely and issues the request with Node's core `http`/`https` module
 * instead — the backend (FastAPI/Starlette) itself has no such
 * restriction and accepts it exactly as documented. See docs/DECISIONS.md
 * D026 for the full rationale.
 */
export function backendGetWithBody(
  path: string,
  headers: Record<string, string>,
  body: string,
): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const url = new URL(backendUrl(path));
    const transport = url.protocol === "https:" ? httpsRequest : httpRequest;
    const req = transport(
      url,
      {
        method: "GET",
        headers: {
          ...headers,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk: Buffer) => {
          data += chunk.toString("utf8");
        });
        res.on("end", () => resolve({ status: res.statusCode ?? 500, text: data }));
      },
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}
