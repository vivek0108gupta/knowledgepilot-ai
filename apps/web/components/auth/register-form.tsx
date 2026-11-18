"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/feedback";
import { TextField } from "@/components/ui/field";
import { DASHBOARD_PATH, LOGIN_PATH } from "@/lib/auth/routes";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

export const MIN_PASSWORD_LENGTH = 8;

const registerSchema = z
  .object({
    email: z.email("Enter a valid email address."),
    password: z.string().min(MIN_PASSWORD_LENGTH, `Use at least ${MIN_PASSWORD_LENGTH} characters.`),
    confirmPassword: z.string(),
  })
  .refine((values) => values.password === values.confirmPassword, {
    path: ["confirmPassword"],
    message: "The passwords do not match.",
  });

type FieldErrors = Partial<Record<"email" | "password" | "confirmPassword", string>>;

function describeSignUpError(error: { code?: string; status?: number; message: string }): string {
  if (error.code === "weak_password") return "Choose a stronger password.";
  if (error.code === "user_already_exists" || error.code === "email_exists") {
    return "An account with this email already exists. Sign in instead.";
  }
  if (error.code === "signup_disabled") return "Sign-ups are currently closed.";
  if (error.status === 429) return "Too many attempts. Wait a minute and try again.";
  if (error.status === undefined || error.status === 0 || error.status >= 500) {
    return "Could not reach the sign-up service. Check your connection and try again.";
  }
  return "Could not create your account. Please try again.";
}

export function RegisterForm() {
  const router = useRouter();
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [confirmationSentTo, setConfirmationSentTo] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const parsed = registerSchema.safeParse({
      email: form.get("email"),
      password: form.get("password"),
      confirmPassword: form.get("confirmPassword"),
    });

    setFormError(null);
    if (!parsed.success) {
      const errors = z.flattenError(parsed.error).fieldErrors;
      setFieldErrors({
        email: errors.email?.[0],
        password: errors.password?.[0],
        confirmPassword: errors.confirmPassword?.[0],
      });
      return;
    }
    setFieldErrors({});
    setSubmitting(true);

    try {
      const { email, password } = parsed.data;
      const { data, error } = await getSupabaseBrowserClient().auth.signUp({
        email,
        password,
        options: { emailRedirectTo: `${window.location.origin}/auth/callback` },
      });
      if (error) {
        setFormError(describeSignUpError(error));
        setSubmitting(false);
        return;
      }
      if (data.session) {
        // Email confirmation is off for this project: the account is ready to use.
        router.replace(DASHBOARD_PATH);
        return;
      }
      setConfirmationSentTo(email);
    } catch (error) {
      setFormError(
        error instanceof Error ? error.message : "Could not create your account. Please try again.",
      );
    }
    setSubmitting(false);
  }

  if (confirmationSentTo) {
    return (
      <div className="flex flex-col gap-4">
        <Alert tone="success">
          Check your inbox. We sent a confirmation link to <strong>{confirmationSentTo}</strong>. Open it to
          finish creating your account.
        </Alert>
        <Link
          href={LOGIN_PATH}
          className="text-center text-sm font-medium text-zinc-900 underline underline-offset-4"
        >
          Back to sign in
        </Link>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
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
        autoComplete="new-password"
        hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
        error={fieldErrors.password}
        disabled={submitting}
      />
      <TextField
        label="Confirm password"
        name="confirmPassword"
        type="password"
        autoComplete="new-password"
        error={fieldErrors.confirmPassword}
        disabled={submitting}
      />

      <Button type="submit" loading={submitting}>
        {submitting ? "Creating account…" : "Create account"}
      </Button>

      <p className="text-center text-sm text-zinc-600">
        Already have an account?{" "}
        <Link href={LOGIN_PATH} className="font-medium text-zinc-900 underline underline-offset-4">
          Sign in
        </Link>
      </p>
    </form>
  );
}
