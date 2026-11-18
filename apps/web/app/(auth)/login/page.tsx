import type { Metadata } from "next";
import { Suspense } from "react";

import { LoginForm } from "@/components/auth/login-form";
import { Skeleton } from "@/components/ui/feedback";

export const metadata: Metadata = { title: "Sign in · KnowledgePilot AI" };

export default function LoginPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight text-zinc-900">Sign in</h1>
        <p className="text-sm text-zinc-600">Welcome back. Enter your details to continue.</p>
      </div>
      {/* The form reads the URL's query string, which is only known at request time. */}
      <Suspense fallback={<Skeleton className="h-64" />}>
        <LoginForm />
      </Suspense>
    </div>
  );
}
