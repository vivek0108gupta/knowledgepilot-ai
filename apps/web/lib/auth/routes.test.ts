import { describe, expect, test } from "vitest";

import { isProtectedPath, resolveAuthRedirect, safeNextPath } from "./routes";

describe("protected routes", () => {
  test.each([
    "/dashboard",
    "/dashboard/workspaces",
    "/dashboard/workspaces/8a5c1f0e-0000-4000-8000-000000000000",
    "/dashboard/settings",
  ])("a signed-out visitor to %s is sent to sign in, and brought back after", (path) => {
    expect(resolveAuthRedirect(path, false)).toBe(`/login?next=${encodeURIComponent(path)}`);
  });

  test.each(["/dashboard", "/dashboard/workspaces", "/dashboard/settings"])(
    "a signed-in user may open %s",
    (path) => {
      expect(resolveAuthRedirect(path, true)).toBeNull();
    },
  );

  test.each(["/login", "/register"])("a signed-in user is sent from %s to the dashboard", (path) => {
    expect(resolveAuthRedirect(path, true)).toBe("/dashboard");
  });

  test.each(["/", "/login", "/register"])("a signed-out visitor may open %s", (path) => {
    expect(resolveAuthRedirect(path, false)).toBeNull();
  });

  test("a path that merely starts with the same letters is not protected", () => {
    expect(isProtectedPath("/dashboards")).toBe(false);
    expect(isProtectedPath("/dashboard-public")).toBe(false);
  });
});

describe("where to go after signing in", () => {
  test("a dashboard path is kept, with its query string", () => {
    expect(safeNextPath("/dashboard/workspaces")).toBe("/dashboard/workspaces");
    expect(safeNextPath("/dashboard/workspaces?tab=members")).toBe("/dashboard/workspaces?tab=members");
  });

  test.each([
    null,
    undefined,
    "",
    "/",
    "/login",
    "https://evil.example",
    "//evil.example",
    "//evil.example/dashboard",
    "/dashboard//evil.example",
    "/\\evil.example",
    "/dashboard\\..\\evil",
    "javascript:alert(1)",
    "dashboard",
  ])("anything else (%j) falls back to the dashboard", (next) => {
    expect(safeNextPath(next)).toBe("/dashboard");
  });
});
