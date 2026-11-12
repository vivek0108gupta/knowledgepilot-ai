import { Suspense } from "react";

import { ChatWorkspace } from "@/components/chat/chat";

export default function Page() {
  return (
    // The workspace and conversation IDs come from the URL, which is only known at request time.
    <Suspense>
      <ChatWorkspace />
    </Suspense>
  );
}
