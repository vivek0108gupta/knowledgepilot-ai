import { Suspense } from "react";

import { UsageDashboard } from "@/components/usage/usage-dashboard";

export default function Page() {
  return (
    // The workspace ID comes from the URL, which is only known at request time.
    <Suspense>
      <UsageDashboard />
    </Suspense>
  );
}
