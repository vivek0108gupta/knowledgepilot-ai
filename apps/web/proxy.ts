import { NextResponse, type NextRequest } from "next/server";

import { resolveAuthRedirect } from "@/lib/auth/routes";
import { checkSession } from "@/lib/supabase/proxy";

/**
 * Runs before the matched routes render (Next.js 16's replacement for `middleware.ts`).
 * This is a convenience for navigation, not the security boundary: the API verifies the
 * access token on every request regardless of what the browser was shown.
 */
export async function proxy(request: NextRequest) {
  const { response, isAuthenticated } = await checkSession(request);

  const destination = resolveAuthRedirect(request.nextUrl.pathname, isAuthenticated);
  if (!destination) return response;

  const redirect = NextResponse.redirect(new URL(destination, request.url));
  // Keep any refreshed session cookies, or the browser would hold on to stale tokens.
  for (const cookie of response.cookies.getAll()) redirect.cookies.set(cookie);
  return redirect;
}

export const config = {
  matcher: ["/dashboard/:path*", "/login", "/register"],
};
