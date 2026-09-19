import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME } from "@/lib/backend";

/**
 * Gate every authenticated page on the presence of the auth cookie. This only checks
 * that a token exists, not that it is still valid — an expired/invalid
 * token is caught by the API route handlers, which surface the backend's
 * real 401 rather than the middleware guessing.
 */
export function proxy(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    const loginUrl = new URL("/login", request.url);
    return NextResponse.redirect(loginUrl);
  }
  return NextResponse.next();
}

export const config = {
  matcher: [
    "/dashboard/:path*",
    "/admin/:path*",
    "/markets/:path*",
    "/strategies/:path*",
    "/autotrade/:path*",
  ],
};
