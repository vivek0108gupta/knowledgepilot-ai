"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { useAuth } from "@/components/auth/auth-provider";
import { Button, buttonClass } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/feedback";
import { WorkspaceList } from "@/components/workspaces/workspace-list";
import { listWorkspaces, workspaceKeys } from "@/lib/workspaces";

import { PageHeader, useCreateWorkspace } from "./shell";

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5">
      <h2 className="text-sm font-medium text-zinc-500">{title}</h2>
      <div className="mt-2">{children}</div>
    </section>
  );
}

export function OverviewPage() {
  const { user } = useAuth();
  const openCreate = useCreateWorkspace();
  const workspaces = useQuery({
    queryKey: workspaceKeys.all,
    queryFn: ({ signal }) => listWorkspaces(signal),
  });

  const owned = workspaces.data?.filter((workspace) => workspace.role === "owner").length;

  return (
    <>
      <PageHeader
        title="Overview"
        description={user?.email ? `Signed in as ${user.email}` : undefined}
        action={<Button onClick={openCreate}>New workspace</Button>}
      />

      <div className="mb-10 grid gap-4 sm:grid-cols-2">
        <Card title="Workspaces you belong to">
          {workspaces.isPending ? (
            <Skeleton className="h-9 w-12" />
          ) : (
            <p className="text-3xl font-semibold text-zinc-900">{workspaces.data?.length ?? "–"}</p>
          )}
        </Card>
        <Card title="Workspaces you own">
          {workspaces.isPending ? (
            <Skeleton className="h-9 w-12" />
          ) : (
            <p className="text-3xl font-semibold text-zinc-900">{owned ?? "–"}</p>
          )}
        </Card>
      </div>

      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="text-base font-semibold text-zinc-900">Recent workspaces</h2>
        <Link href="/dashboard/workspaces" className={buttonClass("ghost", "sm")}>
          View all
        </Link>
      </div>
      <WorkspaceList onCreate={openCreate} limit={3} />
    </>
  );
}

export function WorkspacesPage() {
  const openCreate = useCreateWorkspace();

  return (
    <>
      <PageHeader
        title="Workspaces"
        description="Each workspace has its own documents and members."
        action={<Button onClick={openCreate}>New workspace</Button>}
      />
      <WorkspaceList onCreate={openCreate} />
    </>
  );
}

export function SettingsPage() {
  const { user, signOut } = useAuth();
  const [copied, setCopied] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  async function copyUserId() {
    if (!user) return;
    try {
      await navigator.clipboard.writeText(user.id);
      setCopied(true);
    } catch {
      // Clipboard access was refused; the ID is on screen and can be selected by hand.
      setCopied(false);
    }
  }

  return (
    <>
      <PageHeader title="Settings" description="Your account on KnowledgePilot AI." />

      <div className="flex flex-col gap-6">
        <section className="rounded-xl border border-zinc-200 bg-white p-5">
          <h2 className="text-base font-semibold text-zinc-900">Profile</h2>
          <dl className="mt-4 flex flex-col gap-4 text-sm">
            <div className="flex flex-col gap-1">
              <dt className="text-zinc-500">Email</dt>
              <dd className="break-all text-zinc-900">{user?.email ?? "Not available"}</dd>
            </div>
            <div className="flex flex-col gap-1">
              <dt className="text-zinc-500">User ID</dt>
              <dd className="flex flex-wrap items-center gap-3">
                <code className="rounded-md bg-zinc-100 px-2 py-1 font-mono text-xs break-all text-zinc-900">
                  {user?.id}
                </code>
                <Button variant="secondary" size="sm" onClick={() => void copyUserId()}>
                  {copied ? "Copied" : "Copy"}
                </Button>
                <span role="status" className="sr-only">
                  {copied ? "User ID copied to clipboard" : ""}
                </span>
              </dd>
              <p className="text-zinc-500">
                Share this ID with a workspace owner or admin so they can add you to their workspace.
              </p>
            </div>
          </dl>
        </section>

        <section className="rounded-xl border border-zinc-200 bg-white p-5">
          <h2 className="text-base font-semibold text-zinc-900">Session</h2>
          <p className="mt-1 text-sm text-zinc-600">Sign out of KnowledgePilot AI on this device.</p>
          <Button
            variant="secondary"
            className="mt-4"
            loading={signingOut}
            onClick={() => {
              setSigningOut(true);
              void signOut().finally(() => setSigningOut(false));
            }}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </Button>
        </section>
      </div>
    </>
  );
}
