import { NextRequest } from "next/server";
import { proxyAdminPost } from "@/lib/adminProxy";

/** Proxies `POST /admin/brokers/approval` (Phase 97, D116): approve or hide
 *  the selected brokers on the trading desk. Body forwarded untouched. */
export async function POST(request: NextRequest) {
  return proxyAdminPost(request, "/admin/brokers/approval");
}
