import { z } from "zod";

import { apiFetch } from "./api";

export const roleSchema = z.enum(["owner", "admin", "member"]);
export type Role = z.infer<typeof roleSchema>;

export const ROLE_LABELS: Record<Role, string> = { owner: "Owner", admin: "Admin", member: "Member" };

export const workspaceSchema = z.object({
  id: z.uuid(),
  name: z.string(),
  owner_id: z.uuid(),
  /** The signed-in user's own role in this workspace. */
  role: roleSchema,
  created_at: z.string(),
  updated_at: z.string(),
});
export type Workspace = z.infer<typeof workspaceSchema>;

export const memberSchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  user_id: z.uuid(),
  role: roleSchema,
  created_at: z.string(),
});
export type Member = z.infer<typeof memberSchema>;

/** Mirrors the backend's rule, so mistakes are caught before a request is sent. */
export const workspaceNameSchema = z
  .string()
  .trim()
  .min(1, "Enter a workspace name.")
  .max(100, "Use 100 characters or fewer.");

export const userIdSchema = z.uuid("Enter a valid user ID, for example from their Settings page.");

const noContent = z.void();

/** Query keys in one place, so invalidation always matches what was fetched. */
export const workspaceKeys = {
  all: ["workspaces"] as const,
  detail: (id: string) => ["workspaces", id] as const,
  members: (id: string) => ["workspaces", id, "members"] as const,
};

export const listWorkspaces = (signal?: AbortSignal) =>
  apiFetch("/workspaces", z.array(workspaceSchema), { signal });

export const getWorkspace = (id: string, signal?: AbortSignal) =>
  apiFetch(`/workspaces/${id}`, workspaceSchema, { signal });

export const createWorkspace = (name: string) =>
  apiFetch("/workspaces", workspaceSchema, { method: "POST", body: { name } });

export const renameWorkspace = (id: string, name: string) =>
  apiFetch(`/workspaces/${id}`, workspaceSchema, { method: "PATCH", body: { name } });

export const deleteWorkspace = (id: string) => apiFetch(`/workspaces/${id}`, noContent, { method: "DELETE" });

export const listMembers = (id: string, signal?: AbortSignal) =>
  apiFetch(`/workspaces/${id}/members`, z.array(memberSchema), { signal });

export const addMember = (id: string, userId: string, role: Role) =>
  apiFetch(`/workspaces/${id}/members`, memberSchema, { method: "POST", body: { user_id: userId, role } });

export const removeMember = (id: string, userId: string) =>
  apiFetch(`/workspaces/${id}/members/${userId}`, noContent, { method: "DELETE" });

// What the UI offers each role. The API enforces the same rules; these only decide which
// controls to show, so nobody is offered a button that would be refused.

export const canEditWorkspace = (role: Role) => role === "owner" || role === "admin";

export const canDeleteWorkspace = (role: Role) => role === "owner";

/** The roles this role may grant when adding a member. Empty means it cannot add anyone. */
export function assignableRoles(role: Role): Role[] {
  if (role === "owner") return ["member", "admin", "owner"];
  if (role === "admin") return ["member"];
  return [];
}

export function canRemoveMember(
  actorRole: Role,
  target: Member,
  actorId: string,
  ownerCount: number,
): boolean {
  if (target.role === "owner" && ownerCount <= 1) return false;
  if (target.user_id === actorId) return true;
  if (actorRole === "owner") return true;
  return actorRole === "admin" && target.role === "member";
}
