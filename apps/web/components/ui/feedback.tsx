import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

import { Button } from "./button";

type Tone = "error" | "success" | "info";

const TONES: Record<Tone, string> = {
  error: "border-red-200 bg-red-50 text-red-800",
  success: "border-emerald-200 bg-emerald-50 text-emerald-800",
  info: "border-zinc-200 bg-zinc-50 text-zinc-700",
};

/** An inline message. Errors interrupt screen readers; other tones wait their turn. */
export function Alert({ tone = "error", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn("rounded-lg border px-3 py-2 text-sm", TONES[tone])}
    >
      {children}
    </div>
  );
}

/** A placeholder block shown while content loads. Hidden from assistive technology. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cn("animate-pulse rounded-md bg-zinc-100", className)} />;
}

/** Wraps skeletons so screen readers hear one "Loading" instead of silence. */
export function LoadingRegion({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="status" aria-label={label}>
      {children}
    </div>
  );
}

interface StateProps {
  title: string;
  description: string;
  action?: ReactNode;
}

export function EmptyState({ title, description, action }: StateProps) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-zinc-300 px-6 py-12 text-center">
      <h2 className="text-base font-semibold text-zinc-900">{title}</h2>
      <p className="max-w-sm text-sm text-zinc-600">{description}</p>
      {action}
    </div>
  );
}

export function ErrorState({ title, description, onRetry }: StateProps & { onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-xl border border-red-200 bg-red-50 px-6 py-10 text-center"
    >
      <h2 className="text-base font-semibold text-red-900">{title}</h2>
      <p className="max-w-sm text-sm text-red-800">{description}</p>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

const BADGES = {
  owner: "border-zinc-900 bg-zinc-900 text-white",
  admin: "border-zinc-300 bg-zinc-100 text-zinc-800",
  member: "border-zinc-200 bg-white text-zinc-600",
} as const;

export function Badge({ tone, children }: { tone: keyof typeof BADGES; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        BADGES[tone],
      )}
    >
      {children}
    </span>
  );
}
