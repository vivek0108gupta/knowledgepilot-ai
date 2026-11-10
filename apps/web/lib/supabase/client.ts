import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

import {
  isSupabaseConfigured,
  SUPABASE_NOT_CONFIGURED_MESSAGE,
  SUPABASE_PUBLISHABLE_KEY,
  SUPABASE_URL,
} from "./env";

let client: SupabaseClient | undefined;

/**
 * The Supabase client for browser code. It keeps the session in cookies, so `proxy.ts` sees the
 * same session on the server, and it refreshes the access token before it expires.
 */
export function getSupabaseBrowserClient(): SupabaseClient {
  if (!isSupabaseConfigured) throw new Error(SUPABASE_NOT_CONFIGURED_MESSAGE);
  client ??= createBrowserClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
  return client;
}
