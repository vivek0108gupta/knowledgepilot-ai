"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { configureApiAuth } from "@/lib/api";
import { isProtectedPath, LOGIN_PATH } from "@/lib/auth/routes";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

export type AuthStatus = "loading" | "authenticated" | "unauthenticated";

export interface AuthUser {
  id: string;
  email: string | null;
}

interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Tracks the Supabase session for the whole app and connects it to the API client.
 * Handles the session ending for any reason: the user signs out, the refresh token expires,
 * the API rejects the token, or they sign out in another tab.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<AuthUser | null>(null);
  // True while the user is signing out on purpose, so they are not told their session "expired".
  const signingOut = useRef(false);

  useEffect(() => {
    let supabase: ReturnType<typeof getSupabaseBrowserClient>;
    try {
      supabase = getSupabaseBrowserClient();
    } catch {
      // Not configured: nobody can be signed in. The sign-in form explains why.
      queueMicrotask(() => setStatus("unauthenticated"));
      return;
    }

    configureApiAuth({
      getAccessToken: async () => (await supabase.auth.getSession()).data.session?.access_token ?? null,
      // The API said the token is no longer valid. Drop the local session; the listener
      // below then clears cached data and sends the user to sign in again.
      onUnauthorized: () => void supabase.auth.signOut({ scope: "local" }),
    });

    // Fires once with the stored session on load, then on every sign-in, refresh and sign-out.
    const { data } = supabase.auth.onAuthStateChange((event, session) => {
      if (session) {
        setUser({ id: session.user.id, email: session.user.email ?? null });
        setStatus("authenticated");
        return;
      }

      setUser(null);
      setStatus("unauthenticated");
      if (event !== "SIGNED_OUT") return;

      // Never leave one user's data in memory for the next person on this browser.
      queryClient.clear();
      const expired = !signingOut.current;
      signingOut.current = false;
      if (isProtectedPath(window.location.pathname)) {
        router.replace(expired ? `${LOGIN_PATH}?reason=expired` : LOGIN_PATH);
      }
    });

    return () => {
      data.subscription.unsubscribe();
      configureApiAuth(null);
    };
  }, [queryClient, router]);

  const signOut = useCallback(async () => {
    signingOut.current = true;
    const { error } = await getSupabaseBrowserClient().auth.signOut();
    if (error) {
      // Could not reach Supabase to revoke the session; still sign out on this device.
      await getSupabaseBrowserClient().auth.signOut({ scope: "local" });
    }
  }, []);

  const value = useMemo(() => ({ status, user, signOut }), [status, user, signOut]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>.");
  return value;
}
