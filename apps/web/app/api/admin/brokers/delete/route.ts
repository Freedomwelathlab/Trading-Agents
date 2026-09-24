import { NextRequest } from "next/server";
import { proxyAdminPost } from "@/lib/adminProxy";

/** Proxies `POST /admin/brokers/delete` (Phase 97, D116). The backend
 *  decides per broker whether it is deleted, archived or refused; this
 *  layer forwards the ids and returns that answer verbatim. */
export async function POST(request: NextRequest) {
  return proxyAdminPost(request, "/admin/brokers/delete");
}
