"use client";

import { Dialog } from "@base-ui/react/dialog";
import { Menu } from "@base-ui/react/menu";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { useAuth } from "@/components/auth/auth-provider";
import { Brand } from "@/components/brand";
import { LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { CreateWorkspaceDialog } from "@/components/workspaces/create-workspace-dialog";
import { DASHBOARD_PATH, LOGIN_PATH } from "@/lib/auth/routes";
import { cn } from "@/lib/cn";
import { listWorkspaces, workspaceKeys } from "@/lib/workspaces";

const NAV = [
  { href: DASHBOARD_PATH, label: "Overview" },
  { href: "/dashboard/workspaces", label: "Workspaces" },
  { href: "/dashboard/settings", label: "Settings" },
];

const MENU_POPUP =
  "z-50 min-w-56 rounded-xl border border-zinc-200 bg-white p-1 shadow-lg outline-hidden " +
  "origin-[var(--transform-origin)] transition-[opacity,scale] duration-100 " +
  "data-ending-style:scale-95 data-ending-style:opacity-0 data-starting-style:scale-95 data-starting-style:opacity-0";

const MENU_ITEM =
  "flex cursor-default items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm text-zinc-800 " +
  "outline-hidden select-none data-highlighted:bg-zinc-100";

const MENU_TRIGGER =
  "flex w-full items-center justify-between gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-left " +
  "text-sm hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900";

function Chevron() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 shrink-0 text-zinc-500">
      <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

const CreateWorkspaceContext = createContext<(() => void) | null>(null);

/** Opens the "Create workspace" dialog from anywhere in the dashboard. */
export function useCreateWorkspace(): () => void {
  const open = useContext(CreateWorkspaceContext);
  if (!open) throw new Error("useCreateWorkspace must be used inside the dashboard.");
  return open;
}

function WorkspaceSwitcher({ onNavigate }: { onNavigate: () => void }) {
  const router = useRouter();
  const params = useParams<{ workspaceId?: string }>();
  const openCreate = useCreateWorkspace();
  const workspaces = useQuery({
    queryKey: workspaceKeys.all,
    queryFn: ({ signal }) => listWorkspaces(signal),
  });

  const current = workspaces.data?.find((workspace) => workspace.id === params.workspaceId);

  return (
    <Menu.Root>
      <Menu.Trigger className={MENU_TRIGGER}>
        <span className="flex min-w-0 flex-col">
          <span className="text-xs text-zinc-500">Workspace</span>
          <span className="truncate font-medium text-zinc-900">{current?.name ?? "Select a workspace"}</span>
        </span>
        <Chevron />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner sideOffset={6} align="start" className="z-50 outline-hidden">
          <Menu.Popup className={MENU_POPUP}>
            {workspaces.isPending && <p className="px-3 py-2 text-sm text-zinc-500">Loading…</p>}
            {workspaces.isError && (
              <p className="px-3 py-2 text-sm text-red-700">Could not load workspaces.</p>
            )}
            {workspaces.data?.length === 0 && (
              <p className="px-3 py-2 text-sm text-zinc-500">No workspaces yet.</p>
            )}
            {workspaces.data?.map((workspace) => (
              <Menu.Item
                key={workspace.id}
                className={MENU_ITEM}
                onClick={() => {
                  onNavigate();
                  router.push(`/dashboard/workspaces/${workspace.id}`);
                }}
              >
                <span className="truncate">{workspace.name}</span>
                {workspace.id === current?.id && <span className="text-xs text-zinc-500">Current</span>}
              </Menu.Item>
            ))}
            <Menu.Separator className="mx-1 my-1 h-px bg-zinc-200" />
            <Menu.Item
              className={MENU_ITEM}
              onClick={() => {
                onNavigate();
                openCreate();
              }}
            >
              Create workspace
            </Menu.Item>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

function UserMenu({ onNavigate }: { onNavigate: () => void }) {
  const router = useRouter();
  const { user, signOut } = useAuth();
  const label = user?.email ?? "Account";

  return (
    <Menu.Root>
      <Menu.Trigger className={MENU_TRIGGER} aria-label={`Account menu for ${label}`}>
        <span className="flex min-w-0 items-center gap-2">
          <span
            aria-hidden="true"
            className="grid size-7 shrink-0 place-items-center rounded-full bg-zinc-900 text-xs font-semibold text-white uppercase"
          >
            {label.charAt(0)}
          </span>
          <span className="truncate text-zinc-900">{label}</span>
        </span>
        <Chevron />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner sideOffset={6} align="start" className="z-50 outline-hidden">
          <Menu.Popup className={MENU_POPUP}>
            <Menu.Item
              className={MENU_ITEM}
              onClick={() => {
                onNavigate();
                router.push("/dashboard/settings");
              }}
            >
              Settings
            </Menu.Item>
            <Menu.Separator className="mx-1 my-1 h-px bg-zinc-200" />
            <Menu.Item className={MENU_ITEM} onClick={() => void signOut()}>
              Sign out
            </Menu.Item>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

function isActive(pathname: string, href: string): boolean {
  return href === DASHBOARD_PATH ? pathname === href : pathname === href || pathname.startsWith(`${href}/`);
}

/** The sidebar's contents, shared by the desktop sidebar and the mobile drawer. */
function SidebarContent({ onNavigate }: { onNavigate: () => void }) {
  const pathname = usePathname();

  return (
    <div className="flex h-full flex-col gap-6 p-4">
      <Brand href={DASHBOARD_PATH} className="px-1" />
      <WorkspaceSwitcher onNavigate={onNavigate} />
      <nav aria-label="Dashboard">
        <ul className="flex flex-col gap-1">
          {NAV.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  onClick={onNavigate}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "block rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900",
                    active
                      ? "bg-zinc-100 text-zinc-900"
                      : "text-zinc-600 hover:bg-zinc-50 hover:text-zinc-900",
                  )}
                >
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="mt-auto">
        <UserMenu onNavigate={onNavigate} />
      </div>
    </div>
  );
}

function ShellSkeleton() {
  return (
    <LoadingRegion label="Loading your dashboard">
      <div className="flex min-h-dvh">
        <div className="hidden w-64 flex-col gap-4 border-r border-zinc-200 p-4 lg:flex">
          <Skeleton className="h-7 w-36" />
          <Skeleton className="h-12" />
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
        </div>
        <div className="flex flex-1 flex-col gap-4 p-6 lg:p-10">
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-40" />
        </div>
      </div>
    </LoadingRegion>
  );
}

const noop = () => {};

/** The signed-in frame: sidebar on wide screens, a top bar with a drawer on narrow ones. */
export function DashboardShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { status } = useAuth();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const openCreate = useMemo(() => () => setCreateOpen(true), []);

  // `proxy.ts` already keeps signed-out visitors away. This covers a session that ends
  // while the page is open in a way the auth listener does not redirect for.
  useEffect(() => {
    if (status === "unauthenticated") router.replace(LOGIN_PATH);
  }, [status, router]);

  if (status !== "authenticated") return <ShellSkeleton />;

  return (
    <CreateWorkspaceContext.Provider value={openCreate}>
      <div className="flex min-h-dvh bg-white">
        <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 border-r border-zinc-200 bg-zinc-50/60 lg:block">
          <SidebarContent onNavigate={noop} />
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex items-center justify-between border-b border-zinc-200 bg-white px-4 py-3 lg:hidden">
            <Brand href={DASHBOARD_PATH} />
            <Dialog.Root open={drawerOpen} onOpenChange={setDrawerOpen}>
              <Dialog.Trigger className="rounded-lg border border-zinc-200 px-3 py-1.5 text-sm font-medium text-zinc-900 hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900">
                Menu
              </Dialog.Trigger>
              <Dialog.Portal>
                <Dialog.Backdrop className="fixed inset-0 z-40 bg-zinc-950/40 transition-opacity duration-150 data-ending-style:opacity-0 data-starting-style:opacity-0" />
                <Dialog.Popup className="fixed inset-y-0 left-0 z-50 w-72 max-w-[85vw] border-r border-zinc-200 bg-white shadow-xl transition-transform duration-200 data-ending-style:-translate-x-full data-starting-style:-translate-x-full">
                  <Dialog.Title className="sr-only">Navigation</Dialog.Title>
                  <SidebarContent onNavigate={() => setDrawerOpen(false)} />
                </Dialog.Popup>
              </Dialog.Portal>
            </Dialog.Root>
          </header>

          <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6 lg:px-10 lg:py-10">
            {children}
          </main>
        </div>
      </div>

      <CreateWorkspaceDialog open={createOpen} onOpenChange={setCreateOpen} />
    </CreateWorkspaceContext.Provider>
  );
}

/** A page title with an optional description and action, used at the top of each dashboard page. */
export function PageHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
      <div className="flex min-w-0 flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight break-words text-zinc-900">{title}</h1>
        {description && <p className="text-sm text-zinc-600">{description}</p>}
      </div>
      {action}
    </div>
  );
}
