"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Badge, EmptyState, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { errorMessage } from "@/lib/api";
import { listWorkspaces, ROLE_LABELS, workspaceKeys } from "@/lib/workspaces";

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

interface WorkspaceListProps {
  onCreate: () => void;
  /** Show at most this many, for a summary view. */
  limit?: number;
}

export function WorkspaceList({ onCreate, limit }: WorkspaceListProps) {
  const workspaces = useQuery({
    queryKey: workspaceKeys.all,
    queryFn: ({ signal }) => listWorkspaces(signal),
  });

  if (workspaces.isPending) {
    return (
      <LoadingRegion label="Loading workspaces">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-28 rounded-xl" />
          ))}
        </div>
      </LoadingRegion>
    );
  }

  if (workspaces.isError) {
    return (
      <ErrorState
        title="Could not load your workspaces"
        description={errorMessage(workspaces.error)}
        onRetry={() => void workspaces.refetch()}
      />
    );
  }

  if (workspaces.data.length === 0) {
    return (
      <EmptyState
        title="No workspaces yet"
        description="Create your first workspace to start adding documents and inviting teammates."
        action={<Button onClick={onCreate}>Create workspace</Button>}
      />
    );
  }

  const visible = limit ? workspaces.data.slice(0, limit) : workspaces.data;

  return (
    <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {visible.map((workspace) => (
        <li key={workspace.id}>
          <Link
            href={`/dashboard/workspaces/${workspace.id}`}
            className="flex h-full flex-col gap-3 rounded-xl border border-zinc-200 bg-white p-5 transition-colors hover:border-zinc-300 hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900"
          >
            <div className="flex items-start justify-between gap-3">
              <h3 className="min-w-0 font-medium break-words text-zinc-900">{workspace.name}</h3>
              <Badge tone={workspace.role}>{ROLE_LABELS[workspace.role]}</Badge>
            </div>
            <p className="mt-auto text-sm text-zinc-500">
              Created {dateFormat.format(new Date(workspace.created_at))}
            </p>
          </Link>
        </li>
      ))}
    </ul>
  );
}
