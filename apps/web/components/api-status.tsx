"use client";

import { useQuery } from "@tanstack/react-query";

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
  const { isPending, isError } = useQuery({
    queryKey: ["health", "ready"],
    queryFn: ({ signal }) => getReadiness(signal),
  });

  const status: Status = isPending ? "checking" : isError ? "offline" : "online";

  return (
    <p
      role="status"
      className="inline-flex items-center gap-2 rounded-full border border-zinc-200 px-3 py-1 text-sm text-zinc-700"
    >
      <span aria-hidden="true" className={`size-2 rounded-full ${DOT[status]}`} />
      {LABELS[status]}
    </p>
  );
}
