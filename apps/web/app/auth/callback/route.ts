import { createServerClient } from "@supabase/ssr";
import type { EmailOtpType } from "@supabase/supabase-js";
import { cookies } from "next/headers";
import type { NextRequest } from "next/server";

import { DASHBOARD_PATH, LOGIN_PATH, safeNextPath } from "@/lib/auth/routes";
import { isSupabaseConfigured, SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL } from "@/lib/supabase/env";

const EMAIL_OTP_TYPES: readonly string[] = [
  "signup",
  "invite",
  "magiclink",
  "recovery",
  "email_change",
  "email",
];

/**
 * A redirect to a path on this site. The Location is relative on purpose: behind Docker or a
 * reverse proxy the server sees its own internal address in `request.url`, not the public one,
 * and the browser resolves a relative Location against the address it actually used.
 */
function redirectTo(path: string): Response {
  return new Response(null, { status: 307, headers: { Location: path } });
}

/**
 * Where Supabase sends a user after they click the link in a confirmation email.
 * Turns the one-time code in the URL into a session cookie, then opens the dashboard.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const failed = () => redirectTo(`${LOGIN_PATH}?reason=confirm_failed`);
  if (!isSupabaseConfigured) return failed();

  const cookieStore = await cookies();
  const supabase = createServerClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, {
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll(cookiesToSet) {
        for (const { name, value, options } of cookiesToSet) cookieStore.set(name, value, options);
      },
    },
  });

  const code = searchParams.get("code");
  const tokenHash = searchParams.get("token_hash");
  const type = searchParams.get("type");

  let confirmed = false;
  if (code) {
    confirmed = !(await supabase.auth.exchangeCodeForSession(code)).error;
  } else if (tokenHash && type && EMAIL_OTP_TYPES.includes(type)) {
    confirmed = !(await supabase.auth.verifyOtp({ token_hash: tokenHash, type: type as EmailOtpType })).error;
  }
  if (!confirmed) return failed();

  return redirectTo(safeNextPath(searchParams.get("next") ?? DASHBOARD_PATH));
}
