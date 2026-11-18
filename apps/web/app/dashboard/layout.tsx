import type { Metadata } from "next";
import { Suspense } from "react";

import { DashboardShell } from "@/components/dashboard/shell";

export const metadata: Metadata = { title: "Dashboard · KnowledgePilot AI" };

export default function DashboardLayout({ children }: LayoutProps<"/dashboard">) {
  return (
    // The shell reads the current URL, which is only known at request time.
    <Suspense>
      <DashboardShell>{children}</DashboardShell>
    </Suspense>
  );
}
