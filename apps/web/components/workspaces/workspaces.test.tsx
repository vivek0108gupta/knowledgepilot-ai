import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { Member, Role, Workspace } from "@/lib/workspaces";
import { renderWithQuery } from "@/test/render";

import { CreateWorkspaceDialog } from "./create-workspace-dialog";
import { WorkspaceDetail } from "./workspace-detail";
import { WorkspaceList } from "./workspace-list";

const ME = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const WORKSPACE_ID = "8a5c1f0e-0000-4000-8000-000000000000";

const mocks = vi.hoisted(() => ({
  listWorkspaces: vi.fn(),
  getWorkspace: vi.fn(),
  createWorkspace: vi.fn(),
  renameWorkspace: vi.fn(),
  deleteWorkspace: vi.fn(),
  listMembers: vi.fn(),
  addMember: vi.fn(),
  removeMember: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
}));

vi.mock("@/lib/workspaces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workspaces")>()),
  listWorkspaces: mocks.listWorkspaces,
  getWorkspace: mocks.getWorkspace,
  createWorkspace: mocks.createWorkspace,
  renameWorkspace: mocks.renameWorkspace,
  deleteWorkspace: mocks.deleteWorkspace,
  listMembers: mocks.listMembers,
  addMember: mocks.addMember,
  removeMember: mocks.removeMember,
}));

// Documents have their own tests; here they would only add unrelated requests.
vi.mock("@/components/documents/documents", () => ({ Documents: () => null }));
vi.mock("@/components/search/workspace-search", () => ({ WorkspaceSearch: () => null }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
  useParams: () => ({ workspaceId: WORKSPACE_ID }),
  usePathname: () => `/dashboard/workspaces/${WORKSPACE_ID}`,
}));

vi.mock("@/components/auth/auth-provider", () => ({
  useAuth: () => ({ status: "authenticated", user: { id: ME, email: "ada@example.com" }, signOut: vi.fn() }),
}));

function workspace(role: Role = "owner", name = "Research"): Workspace {
  return {
    id: WORKSPACE_ID,
    name,
    owner_id: ME,
    role,
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
  };
}

function member(userId: string, role: Role): Member {
  return {
    id: `${userId.slice(0, 8)}-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
    workspace_id: WORKSPACE_ID,
    user_id: userId,
    role,
    created_at: "2026-11-11T00:00:00Z",
  };
}

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
});

describe("workspace list", () => {
  test("shows a loading state, then the workspaces with the user's role in each", async () => {
    mocks.listWorkspaces.mockResolvedValue([
      workspace("owner", "Research"),
      { ...workspace("member", "Shared"), id: OTHER },
    ]);
    renderWithQuery(<WorkspaceList onCreate={vi.fn()} />);

    expect(screen.getByRole("status", { name: "Loading workspaces" })).toBeInTheDocument();

    const research = await screen.findByRole("link", { name: /Research/ });
    expect(research).toHaveAttribute("href", `/dashboard/workspaces/${WORKSPACE_ID}`);
    expect(research).toHaveTextContent("Owner");
    expect(screen.getByRole("link", { name: /Shared/ })).toHaveTextContent("Member");
    expect(screen.queryByRole("status", { name: "Loading workspaces" })).not.toBeInTheDocument();
  });

  test("shows an empty state that starts creating a workspace", async () => {
    mocks.listWorkspaces.mockResolvedValue([]);
    const onCreate = vi.fn();
    renderWithQuery(<WorkspaceList onCreate={onCreate} />);

    expect(await screen.findByRole("heading", { name: "No workspaces yet" })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Create workspace" }));

    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  test("shows the error and retries on request", async () => {
    mocks.listWorkspaces.mockRejectedValueOnce(
      new ApiError(0, "network_error", "Could not reach the server."),
    );
    mocks.listWorkspaces.mockResolvedValueOnce([workspace()]);
    renderWithQuery(<WorkspaceList onCreate={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("link", { name: /Research/ })).toBeInTheDocument();
  });

  test("a summary view shows only the first few", async () => {
    mocks.listWorkspaces.mockResolvedValue(
      ["A", "B", "C", "D"].map((name, index) => ({ ...workspace("owner", name), id: `${index}` })),
    );
    renderWithQuery(<WorkspaceList onCreate={vi.fn()} limit={3} />);

    expect(await screen.findAllByRole("link")).toHaveLength(3);
  });
});

describe("create workspace dialog", () => {
  test("creates the workspace, closes, and opens it", async () => {
    mocks.createWorkspace.mockResolvedValue(workspace());
    const onOpenChange = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<CreateWorkspaceDialog open onOpenChange={onOpenChange} />);

    await user.type(await screen.findByLabelText("Workspace name"), "  Research  ");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));

    await vi.waitFor(() => expect(mocks.push).toHaveBeenCalledWith(`/dashboard/workspaces/${WORKSPACE_ID}`));
    expect(mocks.createWorkspace.mock.calls[0]![0]).toBe("Research");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  test("requires a name before sending anything", async () => {
    const user = userEvent.setup();
    renderWithQuery(<CreateWorkspaceDialog open onOpenChange={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Create workspace" }));

    expect(screen.getByLabelText("Workspace name")).toHaveAccessibleDescription("Enter a workspace name.");
    expect(mocks.createWorkspace).not.toHaveBeenCalled();
  });

  test("shows the server's error and stays open", async () => {
    mocks.createWorkspace.mockRejectedValue(
      new ApiError(500, "internal_error", "Something went wrong on our side."),
    );
    const onOpenChange = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<CreateWorkspaceDialog open onOpenChange={onOpenChange} />);

    await user.type(await screen.findByLabelText("Workspace name"), "Research");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on our side.");
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  test("is a labelled dialog that Cancel closes", async () => {
    const onOpenChange = vi.fn();
    renderWithQuery(<CreateWorkspaceDialog open onOpenChange={onOpenChange} />);

    const dialog = await screen.findByRole("dialog", { name: "Create workspace" });
    await userEvent.setup().click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("workspace detail", () => {
  const team = [member(ME, "owner"), member(OTHER, "member")];

  test("an owner can rename, add members, remove members and delete", async () => {
    mocks.getWorkspace.mockResolvedValue(workspace("owner"));
    mocks.listMembers.mockResolvedValue(team);
    renderWithQuery(<WorkspaceDetail />);

    expect(await screen.findByRole("heading", { name: "Research", level: 1 })).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace name")).toHaveValue("Research");
    expect(await screen.findByRole("button", { name: `Remove member ${OTHER}` })).toBeInTheDocument();
    expect(screen.getByLabelText("User ID")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Role")).getAllByRole("option")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Delete workspace" })).toBeInTheDocument();
    // The only owner is not offered a way to leave.
    expect(screen.queryByRole("button", { name: "Leave workspace" })).not.toBeInTheDocument();
  });

  test("an admin can add regular members only, and cannot delete", async () => {
    mocks.getWorkspace.mockResolvedValue(workspace("admin"));
    mocks.listMembers.mockResolvedValue([member(OTHER, "owner"), member(ME, "admin")]);
    renderWithQuery(<WorkspaceDetail />);

    const options = within(await screen.findByLabelText("Role")).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual(["Member"]);
    expect(screen.queryByRole("button", { name: `Remove member ${OTHER}` })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete workspace" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Workspace name")).toBeInTheDocument();
  });

  test("a member sees the members but no management controls, except leaving", async () => {
    mocks.getWorkspace.mockResolvedValue(workspace("member"));
    mocks.listMembers.mockResolvedValue([member(OTHER, "owner"), member(ME, "member")]);
    renderWithQuery(<WorkspaceDetail />);

    expect(await screen.findByRole("button", { name: "Leave workspace" })).toBeInTheDocument();
    expect(screen.getByText(OTHER)).toBeInTheDocument();
    expect(screen.queryByLabelText("Workspace name")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("User ID")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `Remove member ${OTHER}` })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete workspace" })).not.toBeInTheDocument();
  });

  test("adding a member validates the ID, then sends the ID and role", async () => {
    mocks.getWorkspace.mockResolvedValue(workspace("owner"));
    mocks.listMembers.mockResolvedValue(team);
    mocks.addMember.mockResolvedValue(member("33333333-3333-4333-8333-333333333333", "admin"));
    const user = userEvent.setup();
    renderWithQuery(<WorkspaceDetail />);

    const idField = await screen.findByLabelText("User ID");
    await user.type(idField, "ada@example.com");
    await user.click(screen.getByRole("button", { name: "Add member" }));
    expect(idField).toHaveAccessibleDescription(/Enter a valid user ID/);
    expect(mocks.addMember).not.toHaveBeenCalled();

    await user.clear(idField);
    await user.type(idField, "33333333-3333-4333-8333-333333333333");
    await user.selectOptions(screen.getByLabelText("Role"), "admin");
    await user.click(screen.getByRole("button", { name: "Add member" }));

    await vi.waitFor(() =>
      expect(mocks.addMember).toHaveBeenCalledWith(
        WORKSPACE_ID,
        "33333333-3333-4333-8333-333333333333",
        "admin",
      ),
    );
    expect(await screen.findByText("Member added.")).toBeInTheDocument();
  });

  test("shows the server's refusal when adding a member fails", async () => {
    mocks.getWorkspace.mockResolvedValue(workspace("owner"));
    mocks.listMembers.mockResolvedValue(team);
    mocks.addMember.mockRejectedValue(new ApiError(409, "already_member", "This user is already a member."));
    const user = userEvent.setup();
    renderWithQuery(<WorkspaceDetail />);

    await user.type(await screen.findByLabelText("User ID"), OTHER);
    await user.click(screen.getByRole("button", { name: "Add member" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This user is already a member.");
  });

  test("removing a member calls the API for that member", async () => {
    mocks.getWorkspace.mockResolvedValue(workspace("owner"));
    mocks.listMembers.mockResolvedValue(team);
    mocks.removeMember.mockResolvedValue(undefined);
    renderWithQuery(<WorkspaceDetail />);

    await userEvent.setup().click(await screen.findByRole("button", { name: `Remove member ${OTHER}` }));

    await vi.waitFor(() => expect(mocks.removeMember).toHaveBeenCalledWith(WORKSPACE_ID, OTHER));
  });

  test("deleting asks for confirmation first, then returns to the list", async () => {
    mocks.getWorkspace.mockResolvedValue(workspace("owner"));
    mocks.listMembers.mockResolvedValue(team);
    mocks.deleteWorkspace.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderWithQuery(<WorkspaceDetail />);

    await user.click(await screen.findByRole("button", { name: "Delete workspace" }));
    expect(mocks.deleteWorkspace).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog", { name: "Delete “Research”?" });
    // The warning must say documents go too, now that workspaces hold them.
    expect(dialog).toHaveTextContent(
      "This removes the workspace, its documents and its members for everyone.",
    );
    expect(dialog).toHaveTextContent("It cannot be undone.");
    await user.click(within(dialog).getByRole("button", { name: "Delete workspace" }));

    await vi.waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/dashboard/workspaces"));
    expect(mocks.deleteWorkspace).toHaveBeenCalledWith(WORKSPACE_ID);
  });

  test("a workspace the user cannot access shows not found, without a retry", async () => {
    mocks.getWorkspace.mockRejectedValue(new ApiError(404, "workspace_not_found", "Workspace not found."));
    renderWithQuery(<WorkspaceDetail />);

    expect(await screen.findByRole("heading", { name: "Workspace not found" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to workspaces" })).toHaveAttribute(
      "href",
      "/dashboard/workspaces",
    );
    expect(mocks.listMembers).not.toHaveBeenCalled();
  });

  test("a server failure can be retried", async () => {
    mocks.getWorkspace.mockRejectedValue(
      new ApiError(500, "internal_error", "Something went wrong on our side."),
    );
    renderWithQuery(<WorkspaceDetail />);

    expect(await screen.findByRole("heading", { name: "Could not load this workspace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
