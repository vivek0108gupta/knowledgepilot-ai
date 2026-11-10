import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { isSupabaseConfigured, SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL } from "./env";

export interface SessionCheck {
  /** Pass this on (or copy its cookies onto a redirect) so refreshed tokens reach the browser. */
  response: NextResponse;
  isAuthenticated: boolean;
}

/**
 * Checks the session cookie on an incoming request, refreshing an expired access token.
 * `getClaims()` verifies the token's signature, so a forged cookie does not count as signed in.
 */
export async function checkSession(request: NextRequest): Promise<SessionCheck> {
  let response = NextResponse.next({ request });
  if (!isSupabaseConfigured) return { response, isAuthenticated: false };

  const supabase = createServerClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll(cookiesToSet, headers) {
        for (const { name, value } of cookiesToSet) request.cookies.set(name, value);
        response = NextResponse.next({ request });
        for (const { name, value, options } of cookiesToSet) response.cookies.set(name, value, options);
        for (const [key, value] of Object.entries(headers ?? {})) response.headers.set(key, value);
      },
    },
  });

  const { data, error } = await supabase.auth.getClaims();
  return { response, isAuthenticated: !error && Boolean(data?.claims) };
}
