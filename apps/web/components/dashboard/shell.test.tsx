import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import type { AuthStatus } from "@/components/auth/auth-provider";
import { renderWithQuery } from "@/test/render";

import { DashboardShell } from "./shell";

const mocks = vi.hoisted(() => ({
  status: "authenticated" as AuthStatus,
  signOut: vi.fn(),
  replace: vi.fn(),
  push: vi.fn(),
  pathname: "/dashboard",
  listWorkspaces: vi.fn(),
}));

vi.mock("@/components/auth/auth-provider", () => ({
  useAuth: () => ({
    status: mocks.status,
    user: mocks.status === "authenticated" ? { id: "user-1", email: "ada@example.com" } : null,
    signOut: mocks.signOut,
  }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace, push: mocks.push }),
  usePathname: () => mocks.pathname,
  useParams: () => ({}),
}));

vi.mock("@/lib/workspaces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workspaces")>()),
  listWorkspaces: mocks.listWorkspaces,
}));

const WORKSPACE = {
  id: "8a5c1f0e-0000-4000-8000-000000000000",
  name: "Research",
  owner_id: "11111111-1111-4111-8111-111111111111",
  role: "owner" as const,
  created_at: "2026-11-11T00:00:00Z",
  updated_at: "2026-11-11T00:00:00Z",
};

function renderShell() {
  return renderWithQuery(
    <DashboardShell>
      <p>Private dashboard content</p>
    </DashboardShell>,
  );
}

beforeEach(() => {
  mocks.status = "authenticated";
  mocks.pathname = "/dashboard";
  mocks.signOut.mockReset().mockResolvedValue(undefined);
  mocks.replace.mockReset();
  mocks.push.mockReset();
  mocks.listWorkspaces.mockReset().mockResolvedValue([WORKSPACE]);
});

test("a signed-out visitor never sees dashboard content and is sent to sign in", () => {
  mocks.status = "unauthenticated";
  renderShell();

  expect(screen.queryByText("Private dashboard content")).not.toBeInTheDocument();
  expect(mocks.replace).toHaveBeenCalledWith("/login");
  expect(mocks.listWorkspaces).not.toHaveBeenCalled();
});

test("shows a loading state, not content, while the session is being checked", () => {
  mocks.status = "loading";
  renderShell();

  expect(screen.getByRole("status", { name: "Loading your dashboard" })).toBeInTheDocument();
  expect(screen.queryByText("Private dashboard content")).not.toBeInTheDocument();
  expect(mocks.replace).not.toHaveBeenCalled();
});

test("a signed-in user sees the content and navigation", () => {
  renderShell();

  expect(screen.getByText("Private dashboard content")).toBeInTheDocument();
  const nav = screen.getAllByRole("navigation", { name: "Dashboard" })[0]!;
  expect(nav).toHaveTextContent("Overview");
  expect(nav).toHaveTextContent("Workspaces");
  expect(nav).toHaveTextContent("Settings");
  expect(mocks.replace).not.toHaveBeenCalled();
});

test("marks the current page in the navigation, including nested pages", () => {
  mocks.pathname = "/dashboard/workspaces/8a5c1f0e-0000-4000-8000-000000000000";
  renderShell();

  expect(screen.getAllByRole("link", { name: "Workspaces" })[0]).toHaveAttribute("aria-current", "page");
  expect(screen.getAllByRole("link", { name: "Overview" })[0]).not.toHaveAttribute("aria-current");
});

test("the workspace selector lists workspaces and opens the chosen one", async () => {
  const user = userEvent.setup();
  renderShell();

  await user.click(screen.getAllByRole("button", { name: /Workspace/ })[0]!);
  await user.click(await screen.findByRole("menuitem", { name: "Research" }));

  expect(mocks.push).toHaveBeenCalledWith(`/dashboard/workspaces/${WORKSPACE.id}`);
});

test("the workspace selector can open the create dialog", async () => {
  const user = userEvent.setup();
  renderShell();

  await user.click(screen.getAllByRole("button", { name: /Workspace/ })[0]!);
  await user.click(await screen.findByRole("menuitem", { name: "Create workspace" }));

  expect(await screen.findByRole("dialog", { name: "Create workspace" })).toBeInTheDocument();
});

test("the profile menu signs the user out", async () => {
  const user = userEvent.setup();
  renderShell();

  await user.click(screen.getAllByRole("button", { name: "Account menu for ada@example.com" })[0]!);
  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));

  expect(mocks.signOut).toHaveBeenCalledTimes(1);
});
