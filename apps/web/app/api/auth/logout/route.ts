import { NextResponse } from "next/server";
import { AUTH_COOKIE_NAME } from "@/lib/backend";

export async function POST() {
  const res = NextResponse.json({ ok: true });
  res.cookies.delete(AUTH_COOKIE_NAME);
  return res;
}
