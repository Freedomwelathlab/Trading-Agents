import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { AUTH_COOKIE_NAME } from "@/lib/backend";

export default async function Home() {
  const cookieStore = await cookies();
  const hasToken = Boolean(cookieStore.get(AUTH_COOKIE_NAME)?.value);
  redirect(hasToken ? "/dashboard" : "/login");
}
