"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { useAuth } from "@/components/auth/auth-provider";
import { PageHeader } from "@/components/dashboard/shell";
import { Documents } from "@/components/documents/documents";
import { Button, buttonClass } from "@/components/ui/button";
import { Alert, Badge, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { SelectField, TextField } from "@/components/ui/field";
import { Modal } from "@/components/ui/modal";
import { ApiError, errorMessage } from "@/lib/api";
import {
  addMember,
  assignableRoles,
  canDeleteWorkspace,
  canEditWorkspace,
  canRemoveMember,
  deleteWorkspace,
  getWorkspace,
  listMembers,
  removeMember,
  renameWorkspace,
  ROLE_LABELS,
  roleSchema,
  userIdSchema,
  workspaceKeys,
  workspaceNameSchema,
  type Member,
  type Workspace,
} from "@/lib/workspaces";

const WORKSPACES_PATH = "/dashboard/workspaces";

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5">
      <h2 className="text-base font-semibold text-zinc-900">{title}</h2>
      {description && <p className="mt-1 text-sm text-zinc-600">{description}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function RenameForm({ workspace }: { workspace: Workspace }) {
  const queryClient = useQueryClient();
  const [nameError, setNameError] = useState<string | undefined>();

  const rename = useMutation({
    mutationFn: (name: string) => renameWorkspace(workspace.id, name),
    onSuccess: (updated) => {
      queryClient.setQueryData(workspaceKeys.detail(workspace.id), updated);
      return queryClient.invalidateQueries({ queryKey: workspaceKeys.all, exact: true });
    },
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = workspaceNameSchema.safeParse(new FormData(event.currentTarget).get("name"));
    if (!parsed.success) {
      setNameError(parsed.error.issues[0]?.message);
      return;
    }
    setNameError(undefined);
    rename.mutate(parsed.data);
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-3">
      {rename.isError && <Alert>{errorMessage(rename.error)}</Alert>}
      {rename.isSuccess && <Alert tone="success">Workspace renamed.</Alert>}
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1 basis-64">
          <TextField
            // Remount when the saved name changes so the field shows it.
            key={workspace.name}
            label="Workspace name"
            name="name"
            defaultValue={workspace.name}
            maxLength={100}
            autoComplete="off"
            error={nameError}
            disabled={rename.isPending}
          />
        </div>
        <Button type="submit" variant="secondary" loading={rename.isPending}>
          {rename.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </form>
  );
}

function AddMemberForm({ workspace }: { workspace: Workspace }) {
  const queryClient = useQueryClient();
  const roles = assignableRoles(workspace.role);
  const [idError, setIdError] = useState<string | undefined>();

  const add = useMutation({
    mutationFn: (input: { userId: string; role: Member["role"] }) =>
      addMember(workspace.id, input.userId, input.role),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: workspaceKeys.members(workspace.id) }),
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const userId = userIdSchema.safeParse(String(form.get("userId") ?? "").trim());
    const role = roleSchema.safeParse(form.get("role"));
    if (!userId.success || !role.success) {
      setIdError(userId.success ? undefined : userId.error.issues[0]?.message);
      return;
    }
    setIdError(undefined);
    add.mutate({ userId: userId.data, role: role.data }, { onSuccess: () => formElement.reset() });
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-3 border-t border-zinc-200 pt-4">
      <h3 className="text-sm font-medium text-zinc-900">Add a member</h3>
      {add.isError && <Alert>{errorMessage(add.error)}</Alert>}
      {add.isSuccess && <Alert tone="success">Member added.</Alert>}
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1 basis-72">
          <TextField
            label="User ID"
            name="userId"
            placeholder="00000000-0000-0000-0000-000000000000"
            hint="They can find it on their Settings page."
            autoComplete="off"
            spellCheck={false}
            error={idError}
            disabled={add.isPending}
          />
        </div>
        <div className="w-36">
          <SelectField label="Role" name="role" defaultValue="member" disabled={add.isPending}>
            {roles.map((role) => (
              <option key={role} value={role}>
                {ROLE_LABELS[role]}
              </option>
            ))}
          </SelectField>
        </div>
        <Button type="submit" loading={add.isPending} className="mt-[1.625rem]">
          {add.isPending ? "Adding…" : "Add member"}
        </Button>
      </div>
    </form>
  );
}

function Members({ workspace }: { workspace: Workspace }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const members = useQuery({
    queryKey: workspaceKeys.members(workspace.id),
    queryFn: ({ signal }) => listMembers(workspace.id, signal),
  });

  const remove = useMutation({
    mutationFn: (member: Member) => removeMember(workspace.id, member.user_id),
    onSuccess: async (_result, member) => {
      if (member.user_id === user?.id) {
        // They left: this workspace is no longer theirs to see.
        queryClient.removeQueries({ queryKey: workspaceKeys.detail(workspace.id) });
        await queryClient.invalidateQueries({ queryKey: workspaceKeys.all, exact: true });
        router.replace(WORKSPACES_PATH);
        return;
      }
      await queryClient.invalidateQueries({ queryKey: workspaceKeys.members(workspace.id) });
    },
  });

  if (members.isPending) {
    return (
      <LoadingRegion label="Loading members">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
        </div>
      </LoadingRegion>
    );
  }
  if (members.isError) {
    return (
      <ErrorState
        title="Could not load members"
        description={errorMessage(members.error)}
        onRetry={() => void members.refetch()}
      />
    );
  }

  const ownerCount = members.data.filter((member) => member.role === "owner").length;

  return (
    <div className="flex flex-col gap-4">
      {remove.isError && <Alert>{errorMessage(remove.error)}</Alert>}
      <ul className="divide-y divide-zinc-200 rounded-lg border border-zinc-200">
        {members.data.map((member) => {
          const isSelf = member.user_id === user?.id;
          const removable = user ? canRemoveMember(workspace.role, member, user.id, ownerCount) : false;
          const removing = remove.isPending && remove.variables?.id === member.id;
          return (
            <li key={member.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="text-sm font-medium text-zinc-900">
                  {isSelf ? (user?.email ?? "You") : "Member"}
                  {isSelf && <span className="font-normal text-zinc-500"> (you)</span>}
                </span>
                <code className="font-mono text-xs break-all text-zinc-500">{member.user_id}</code>
              </div>
              <div className="flex items-center gap-3">
                <Badge tone={member.role}>{ROLE_LABELS[member.role]}</Badge>
                {removable && (
                  <Button
                    variant="ghost"
                    size="sm"
                    loading={removing}
                    disabled={remove.isPending}
                    aria-label={isSelf ? "Leave workspace" : `Remove member ${member.user_id}`}
                    onClick={() => remove.mutate(member)}
                  >
                    {removing ? "Removing…" : isSelf ? "Leave" : "Remove"}
                  </Button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {assignableRoles(workspace.role).length > 0 && <AddMemberForm workspace={workspace} />}
    </div>
  );
}

function DeleteWorkspace({ workspace }: { workspace: Workspace }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const remove = useMutation({
    mutationFn: () => deleteWorkspace(workspace.id),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: workspaceKeys.detail(workspace.id) });
      await queryClient.invalidateQueries({ queryKey: workspaceKeys.all, exact: true });
      router.replace(WORKSPACES_PATH);
    },
  });

  return (
    <>
      <Button variant="danger" onClick={() => setOpen(true)}>
        Delete workspace
      </Button>
      <Modal
        open={open}
        onOpenChange={(next) => {
          if (!next) remove.reset();
          setOpen(next);
        }}
        title={`Delete “${workspace.name}”?`}
        description="This removes the workspace and all of its members for everyone. It cannot be undone."
      >
        {remove.isError && <Alert>{errorMessage(remove.error)}</Alert>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setOpen(false)} disabled={remove.isPending}>
            Cancel
          </Button>
          <Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>
            {remove.isPending ? "Deleting…" : "Delete workspace"}
          </Button>
        </div>
      </Modal>
    </>
  );
}

export function WorkspaceDetail() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const workspace = useQuery({
    queryKey: workspaceKeys.detail(workspaceId),
    queryFn: ({ signal }) => getWorkspace(workspaceId, signal),
  });

  if (workspace.isPending) {
    return (
      <LoadingRegion label="Loading workspace">
        <div className="flex flex-col gap-6">
          <Skeleton className="h-9 w-64" />
          <Skeleton className="h-32 rounded-xl" />
          <Skeleton className="h-48 rounded-xl" />
        </div>
      </LoadingRegion>
    );
  }

  if (workspace.isError) {
    // 404 covers both "does not exist" and "you are not a member"; 422 is a malformed ID in the URL.
    const missing = workspace.error instanceof ApiError && [404, 422].includes(workspace.error.status);
    return (
      <div className="flex flex-col items-center gap-4">
        <div className="w-full">
          <ErrorState
            title={missing ? "Workspace not found" : "Could not load this workspace"}
            description={
              missing
                ? "It may have been deleted, or you may not be a member of it."
                : errorMessage(workspace.error)
            }
            onRetry={missing ? undefined : () => void workspace.refetch()}
          />
        </div>
        <Link href={WORKSPACES_PATH} className={buttonClass("secondary")}>
          Back to workspaces
        </Link>
      </div>
    );
  }

  const data = workspace.data;

  return (
    <>
      <PageHeader
        title={data.name}
        description={`Your role: ${ROLE_LABELS[data.role]}`}
        action={
          <Link href={WORKSPACES_PATH} className={buttonClass("secondary", "sm")}>
            All workspaces
          </Link>
        }
      />
      <div className="flex flex-col gap-6">
        <Documents workspace={data} />
        {canEditWorkspace(data.role) && (
          <Section title="General">
            <RenameForm workspace={data} />
          </Section>
        )}
        <Section title="Members" description="People who can open this workspace.">
          <Members workspace={data} />
        </Section>
        {canDeleteWorkspace(data.role) && (
          <Section title="Danger zone" description="Deleting a workspace is permanent.">
            <DeleteWorkspace workspace={data} />
          </Section>
        )}
      </div>
    </>
  );
}
