import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { z } from "zod";

import { apiFetch } from "@/lib/api";
import { renderWithQuery } from "@/test/render";

import { AuthProvider, useAuth } from "./auth-provider";

type Session = { access_token: string; user: { id: string; email?: string } } | null;
type Listener = (event: string, session: Session) => void;

const mocks = vi.hoisted(() => ({
  listener: null as Listener | null,
  session: null as Session,
  signOut: vi.fn(),
  unsubscribe: vi.fn(),
  replace: vi.fn(),
  configured: true,
}));

vi.mock("@/lib/supabase/client", () => ({
  getSupabaseBrowserClient: () => {
    if (!mocks.configured) throw new Error("Sign-in is not configured.");
    return {
      auth: {
        onAuthStateChange: (listener: Listener) => {
          mocks.listener = listener;
          return { data: { subscription: { unsubscribe: mocks.unsubscribe } } };
        },
        getSession: async () => ({ data: { session: mocks.session } }),
        signOut: mocks.signOut,
      },
    };
  },
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: mocks.replace }) }));

const ADA: Session = { access_token: "ada-token", user: { id: "user-1", email: "ada@example.com" } };

/** Simulates Supabase reporting a session change. */
function emit(event: string, session: Session) {
  mocks.session = session;
  act(() => mocks.listener?.(event, session));
}

function Probe() {
  const { status, user, signOut } = useAuth();
  return (
    <div>
      <p>status: {status}</p>
      <p>email: {user?.email ?? "none"}</p>
      <button onClick={() => void signOut()}>Sign out</button>
    </div>
  );
}

function renderProvider() {
  return renderWithQuery(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
}

beforeEach(() => {
  mocks.listener = null;
  mocks.session = null;
  mocks.configured = true;
  mocks.signOut.mockReset().mockResolvedValue({ error: null });
  mocks.unsubscribe.mockReset();
  mocks.replace.mockReset();
});

test("is loading until Supabase reports the stored session", () => {
  renderProvider();

  expect(screen.getByText("status: loading")).toBeInTheDocument();
});

test("becomes authenticated when a session is restored", () => {
  renderProvider();

  emit("INITIAL_SESSION", ADA);

  expect(screen.getByText("status: authenticated")).toBeInTheDocument();
  expect(screen.getByText("email: ada@example.com")).toBeInTheDocument();
});

test("becomes unauthenticated when there is no session, without redirecting", () => {
  window.history.replaceState({}, "", "/dashboard");
  renderProvider();

  emit("INITIAL_SESSION", null);

  expect(screen.getByText("status: unauthenticated")).toBeInTheDocument();
  // Only an actual sign-out redirects; the first load is `proxy.ts`'s job.
  expect(mocks.replace).not.toHaveBeenCalled();
});

test("stays signed in when the access token is refreshed", () => {
  renderProvider();
  emit("INITIAL_SESSION", ADA);

  emit("TOKEN_REFRESHED", { ...ADA!, access_token: "newer-token" });

  expect(screen.getByText("status: authenticated")).toBeInTheDocument();
  expect(mocks.replace).not.toHaveBeenCalled();
});

test("an expired session clears cached data and sends the user to sign in with an explanation", () => {
  window.history.replaceState({}, "", "/dashboard/workspaces");
  const { queryClient } = renderProvider();
  emit("INITIAL_SESSION", ADA);
  queryClient.setQueryData(["workspaces"], [{ name: "Ada's private workspace" }]);

  emit("SIGNED_OUT", null);

  expect(screen.getByText("status: unauthenticated")).toBeInTheDocument();
  expect(queryClient.getQueryData(["workspaces"])).toBeUndefined();
  expect(mocks.replace).toHaveBeenCalledWith("/login?reason=expired");
});

test("signing out on purpose goes to sign in without the expired notice", async () => {
  window.history.replaceState({}, "", "/dashboard");
  mocks.signOut.mockImplementation(async () => {
    emit("SIGNED_OUT", null);
    return { error: null };
  });
  renderProvider();
  emit("INITIAL_SESSION", ADA);

  await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));

  expect(mocks.replace).toHaveBeenCalledWith("/login");
});

test("a sign-out on a public page does not redirect", () => {
  window.history.replaceState({}, "", "/");
  renderProvider();
  emit("INITIAL_SESSION", ADA);

  emit("SIGNED_OUT", null);

  expect(mocks.replace).not.toHaveBeenCalled();
});

test("API requests carry the current session's token", async () => {
  const fetchMock = vi.fn(async () => Response.json({}));
  vi.stubGlobal("fetch", fetchMock);
  renderProvider();
  emit("INITIAL_SESSION", ADA);

  await apiFetch("/me", z.object({}));

  const headers = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].headers;
  expect(headers).toMatchObject({ Authorization: "Bearer ada-token" });
  vi.unstubAllGlobals();
});

test("a 401 from the API ends the local session", async () => {
  vi.stubGlobal("fetch", async () =>
    Response.json({ error: { code: "token_expired", message: "Expired." } }, { status: 401 }),
  );
  renderProvider();
  emit("INITIAL_SESSION", ADA);

  await apiFetch("/me", z.object({})).catch(() => {});

  expect(mocks.signOut).toHaveBeenCalledWith({ scope: "local" });
  vi.unstubAllGlobals();
});

test("falls back to signed out when sign-in is not configured", async () => {
  mocks.configured = false;
  renderProvider();

  expect(await screen.findByText("status: unauthenticated")).toBeInTheDocument();
});

test("stops listening when unmounted", () => {
  const { unmount } = renderProvider();

  unmount();

  expect(mocks.unsubscribe).toHaveBeenCalledTimes(1);
});
