"use client";

import { useEffect, useState } from "react";

import { getReadiness } from "@/lib/api";

type Status = "checking" | "online" | "offline";

const LABELS: Record<Status, string> = {
  checking: "Checking API…",
  online: "API online",
  offline: "API unreachable",
};

const DOT: Record<Status, string> = {
  checking: "bg-zinc-400",
  online: "bg-emerald-500",
  offline: "bg-red-500",
};

/** Shows whether the browser can reach the backend and the backend can reach its database. */
export function ApiStatus() {
  const [status, setStatus] = useState<Status>("checking");

  useEffect(() => {
    const controller = new AbortController();

    getReadiness({ signal: controller.signal })
      .then(() => setStatus("online"))
      .catch(() => {
        if (!controller.signal.aborted) setStatus("offline");
      });

    return () => controller.abort();
  }, []);

  return (
    <p
      role="status"
      className="inline-flex items-center gap-2 rounded-full border border-zinc-200 px-3 py-1 text-sm text-zinc-700 dark:border-zinc-800 dark:text-zinc-300"
    >
      <span aria-hidden="true" className={`size-2 rounded-full ${DOT[status]}`} />
      {LABELS[status]}
    </p>
  );
}
