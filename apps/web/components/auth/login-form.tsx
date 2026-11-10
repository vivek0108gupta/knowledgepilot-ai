"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/feedback";
import { TextField } from "@/components/ui/field";
import { REGISTER_PATH, safeNextPath } from "@/lib/auth/routes";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

const loginSchema = z.object({
  email: z.email("Enter a valid email address."),
  password: z.string().min(1, "Enter your password."),
});

type FieldErrors = Partial<Record<"email" | "password", string>>;

const NOTICES: Record<string, string> = {
  expired: "Your session expired. Sign in again to continue.",
  confirm_failed: "That confirmation link is invalid or has expired. Sign in, or register again.",
};

/** Turns Supabase's error into something a person can act on, without revealing which part was wrong. */
function describeSignInError(error: { code?: string; status?: number; message: string }): string {
  if (error.code === "invalid_credentials") return "Incorrect email or password.";
  if (error.code === "email_not_confirmed")
    return "Confirm your email first. Check your inbox for the link we sent.";
  if (error.status === 429) return "Too many attempts. Wait a minute and try again.";
  if (error.status === undefined || error.status === 0 || error.status >= 500) {
    return "Could not reach the sign-in service. Check your connection and try again.";
  }
  return "Could not sign you in. Please try again.";
}

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const notice = NOTICES[searchParams.get("reason") ?? ""];

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const parsed = loginSchema.safeParse({ email: form.get("email"), password: form.get("password") });

    setFormError(null);
    if (!parsed.success) {
      const errors = z.flattenError(parsed.error).fieldErrors;
      setFieldErrors({ email: errors.email?.[0], password: errors.password?.[0] });
      return;
    }
    setFieldErrors({});
    setSubmitting(true);

    try {
      const { error } = await getSupabaseBrowserClient().auth.signInWithPassword(parsed.data);
      if (error) {
        setFormError(describeSignInError(error));
        setSubmitting(false);
        return;
      }
      // Stay in the loading state while the dashboard loads.
      router.replace(safeNextPath(searchParams.get("next")));
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Could not sign you in. Please try again.");
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
      {notice && !formError && <Alert tone="info">{notice}</Alert>}
      {formError && <Alert>{formError}</Alert>}

      <TextField
        label="Email"
        name="email"
        type="email"
        autoComplete="email"
        inputMode="email"
        error={fieldErrors.email}
        disabled={submitting}
      />
      <TextField
        label="Password"
        name="password"
        type="password"
        autoComplete="current-password"
        error={fieldErrors.password}
        disabled={submitting}
      />

      <Button type="submit" loading={submitting}>
        {submitting ? "Signing in…" : "Sign in"}
      </Button>

      <p className="text-center text-sm text-zinc-600">
        New here?{" "}
        <Link href={REGISTER_PATH} className="font-medium text-zinc-900 underline underline-offset-4">
          Create an account
        </Link>
      </p>
    </form>
  );
}
