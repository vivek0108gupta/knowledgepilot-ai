import Link from "next/link";

import { cn } from "@/lib/cn";

/** The product name with its mark, linking home. */
export function Brand({ href = "/", className }: { href?: string; className?: string }) {
  return (
    <Link
      href={href}
      className={cn(
        "inline-flex items-center gap-2 rounded-md text-sm font-semibold tracking-tight text-zinc-900",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className="grid size-7 place-items-center rounded-lg bg-zinc-900 text-xs font-bold text-white"
      >
        K
      </span>
      KnowledgePilot
    </Link>
  );
}
