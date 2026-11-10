import { Suspense } from "react";

import { WorkspaceDetail } from "@/components/workspaces/workspace-detail";

export default function Page() {
  return (
    // The workspace ID comes from the URL, which is only known at request time.
    <Suspense>
      <WorkspaceDetail />
    </Suspense>
  );
}
