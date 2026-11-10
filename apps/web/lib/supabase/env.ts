// Both values are public by design: they ship in the browser bundle. The publishable key only
// identifies the project; what a user may do is decided by their verified session.
export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
export const SUPABASE_PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? "";

export const isSupabaseConfigured = SUPABASE_URL !== "" && SUPABASE_PUBLISHABLE_KEY !== "";

export const SUPABASE_NOT_CONFIGURED_MESSAGE =
  "Sign-in is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY.";
