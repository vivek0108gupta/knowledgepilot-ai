import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { LoginForm } from "./login-form";

const mocks = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  replace: vi.fn(),
  search: new URLSearchParams(),
}));

vi.mock("@/lib/supabase/client", () => ({
  getSupabaseBrowserClient: () => ({ auth: { signInWithPassword: mocks.signInWithPassword } }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace }),
  useSearchParams: () => mocks.search,
}));

beforeEach(() => {
  mocks.signInWithPassword.mockReset().mockResolvedValue({ error: null });
  mocks.replace.mockReset();
  mocks.search = new URLSearchParams();
});

async function submit(email: string, password: string) {
  const user = userEvent.setup();
  if (email) await user.type(screen.getByLabelText("Email"), email);
  if (password) await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

test("signs in with the entered credentials and opens the dashboard", async () => {
  render(<LoginForm />);

  await submit("ada@example.com", "correct horse");

  expect(mocks.signInWithPassword).toHaveBeenCalledWith({
    email: "ada@example.com",
    password: "correct horse",
  });
  expect(mocks.replace).toHaveBeenCalledWith("/dashboard");
});

test("returns the user to the page they were sent from", async () => {
  mocks.search = new URLSearchParams({ next: "/dashboard/settings" });
  render(<LoginForm />);

  await submit("ada@example.com", "correct horse");

  expect(mocks.replace).toHaveBeenCalledWith("/dashboard/settings");
});

test("ignores a next parameter that points off-site", async () => {
  mocks.search = new URLSearchParams({ next: "https://evil.example/dashboard" });
  render(<LoginForm />);

  await submit("ada@example.com", "correct horse");

  expect(mocks.replace).toHaveBeenCalledWith("/dashboard");
});

test("explains what is missing without contacting Supabase", async () => {
  render(<LoginForm />);

  await submit("", "");

  expect(screen.getByLabelText("Email")).toHaveAccessibleDescription("Enter a valid email address.");
  expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("Enter your password.");
  expect(screen.getByLabelText("Email")).toBeInvalid();
  expect(mocks.signInWithPassword).not.toHaveBeenCalled();
});

test("rejects a malformed email", async () => {
  render(<LoginForm />);

  await submit("not-an-email", "secret");

  expect(screen.getByLabelText("Email")).toHaveAccessibleDescription("Enter a valid email address.");
  expect(mocks.signInWithPassword).not.toHaveBeenCalled();
});

test("shows a wrong-credentials error and lets the user try again", async () => {
  mocks.signInWithPassword.mockResolvedValue({
    error: { code: "invalid_credentials", status: 400, message: "Invalid login credentials" },
  });
  render(<LoginForm />);

  await submit("ada@example.com", "wrong");

  expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
  expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  expect(mocks.replace).not.toHaveBeenCalled();
});

test.each([
  [{ code: "email_not_confirmed", status: 400, message: "x" }, "Confirm your email first."],
  [{ status: 429, message: "x" }, "Too many attempts."],
  [{ status: 500, message: "x" }, "Could not reach the sign-in service."],
  [
    { name: "AuthRetryableFetchError", status: 0, message: "Failed to fetch" },
    "Could not reach the sign-in service.",
  ],
])("explains the failure %j", async (error, expected) => {
  mocks.signInWithPassword.mockResolvedValue({ error });
  render(<LoginForm />);

  await submit("ada@example.com", "secret");

  expect(await screen.findByRole("alert")).toHaveTextContent(expected);
});

test("shows a loading state and blocks a second submit while signing in", async () => {
  let finish: (value: { error: null }) => void = () => {};
  mocks.signInWithPassword.mockReturnValue(new Promise((resolve) => (finish = resolve)));
  render(<LoginForm />);

  await submit("ada@example.com", "secret");

  const button = screen.getByRole("button", { name: "Signing in…" });
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute("aria-busy", "true");
  expect(screen.getByLabelText("Email")).toBeDisabled();

  finish({ error: null });
  await vi.waitFor(() => expect(mocks.replace).toHaveBeenCalled());
  expect(mocks.signInWithPassword).toHaveBeenCalledTimes(1);
});

test("tells a user whose session expired why they are here", () => {
  mocks.search = new URLSearchParams({ reason: "expired" });
  render(<LoginForm />);

  expect(screen.getByRole("status")).toHaveTextContent("Your session expired.");
});

test("reports when sign-in is not configured", async () => {
  mocks.signInWithPassword.mockRejectedValue(new Error("Sign-in is not configured."));
  render(<LoginForm />);

  await submit("ada@example.com", "secret");

  expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in is not configured.");
});
