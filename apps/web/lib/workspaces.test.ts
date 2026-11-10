import { describe, expect, test } from "vitest";

import {
  assignableRoles,
  canDeleteWorkspace,
  canEditWorkspace,
  canRemoveMember,
  userIdSchema,
  workspaceNameSchema,
  type Member,
  type Role,
} from "./workspaces";

const ME = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";

function member(role: Role, userId = OTHER): Member {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    workspace_id: "44444444-4444-4444-8444-444444444444",
    user_id: userId,
    role,
    created_at: "2026-11-11T00:00:00Z",
  };
}

describe("what each role is offered", () => {
  test("only owners and admins can rename", () => {
    expect([canEditWorkspace("owner"), canEditWorkspace("admin"), canEditWorkspace("member")]).toEqual([
      true,
      true,
      false,
    ]);
  });

  test("only owners can delete", () => {
    expect([canDeleteWorkspace("owner"), canDeleteWorkspace("admin"), canDeleteWorkspace("member")]).toEqual([
      true,
      false,
      false,
    ]);
  });

  test("owners can grant any role, admins only member, members none", () => {
    expect(assignableRoles("owner")).toEqual(["member", "admin", "owner"]);
    expect(assignableRoles("admin")).toEqual(["member"]);
    expect(assignableRoles("member")).toEqual([]);
  });
});

describe("who can remove whom", () => {
  test("an owner can remove admins, members and other owners", () => {
    expect(canRemoveMember("owner", member("admin"), ME, 2)).toBe(true);
    expect(canRemoveMember("owner", member("member"), ME, 2)).toBe(true);
    expect(canRemoveMember("owner", member("owner"), ME, 2)).toBe(true);
  });

  test("an admin can remove members, but not admins or owners", () => {
    expect(canRemoveMember("admin", member("member"), ME, 1)).toBe(true);
    expect(canRemoveMember("admin", member("admin"), ME, 1)).toBe(false);
    expect(canRemoveMember("admin", member("owner"), ME, 2)).toBe(false);
  });

  test("a member can remove nobody but themself", () => {
    expect(canRemoveMember("member", member("member"), ME, 1)).toBe(false);
    expect(canRemoveMember("member", member("admin"), ME, 1)).toBe(false);
    expect(canRemoveMember("member", member("member", ME), ME, 1)).toBe(true);
  });

  test("the last owner cannot be removed, even by themself", () => {
    expect(canRemoveMember("owner", member("owner", ME), ME, 1)).toBe(false);
    expect(canRemoveMember("owner", member("owner", ME), ME, 2)).toBe(true);
  });
});

describe("input rules", () => {
  test("workspace names are trimmed and must be 1 to 100 characters", () => {
    expect(workspaceNameSchema.parse("  Research  ")).toBe("Research");
    expect(workspaceNameSchema.safeParse("   ").success).toBe(false);
    expect(workspaceNameSchema.safeParse("x".repeat(100)).success).toBe(true);
    expect(workspaceNameSchema.safeParse("x".repeat(101)).success).toBe(false);
  });

  test("a user ID must be a UUID", () => {
    expect(userIdSchema.safeParse(ME).success).toBe(true);
    expect(userIdSchema.safeParse("ada@example.com").success).toBe(false);
  });
});
