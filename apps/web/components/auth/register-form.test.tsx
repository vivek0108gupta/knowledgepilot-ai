import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { RegisterForm } from "./register-form";

const mocks = vi.hoisted(() => ({ signUp: vi.fn(), replace: vi.fn() }));

vi.mock("@/lib/supabase/client", () => ({
  getSupabaseBrowserClient: () => ({ auth: { signUp: mocks.signUp } }),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: mocks.replace }) }));

beforeEach(() => {
  mocks.signUp.mockReset().mockResolvedValue({ data: { session: null }, error: null });
  mocks.replace.mockReset();
});

async function submit(email: string, password: string, confirm = password) {
  const user = userEvent.setup();
  if (email) await user.type(screen.getByLabelText("Email"), email);
  if (password) await user.type(screen.getByLabelText("Password"), password);
  if (confirm) await user.type(screen.getByLabelText("Confirm password"), confirm);
  await user.click(screen.getByRole("button", { name: "Create account" }));
}

test("creates the account and asks the user to confirm their email", async () => {
  render(<RegisterForm />);

  await submit("ada@example.com", "long enough password");

  expect(mocks.signUp).toHaveBeenCalledWith({
    email: "ada@example.com",
    password: "long enough password",
    options: { emailRedirectTo: `${window.location.origin}/auth/callback` },
  });
  expect(await screen.findByRole("status")).toHaveTextContent(
    "We sent a confirmation link to ada@example.com",
  );
  expect(screen.queryByLabelText("Email")).not.toBeInTheDocument();
  expect(mocks.replace).not.toHaveBeenCalled();
});

test("opens the dashboard straight away when no confirmation is needed", async () => {
  mocks.signUp.mockResolvedValue({ data: { session: { access_token: "t" } }, error: null });
  render(<RegisterForm />);

  await submit("ada@example.com", "long enough password");

  expect(mocks.replace).toHaveBeenCalledWith("/dashboard");
});

test("rejects a password shorter than 8 characters", async () => {
  render(<RegisterForm />);

  await submit("ada@example.com", "short");

  expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("Use at least 8 characters.");
  expect(mocks.signUp).not.toHaveBeenCalled();
});

test("rejects passwords that do not match", async () => {
  render(<RegisterForm />);

  await submit("ada@example.com", "long enough password", "a different password");

  expect(screen.getByLabelText("Confirm password")).toHaveAccessibleDescription(
    "The passwords do not match.",
  );
  expect(mocks.signUp).not.toHaveBeenCalled();
});

test("rejects a malformed email", async () => {
  render(<RegisterForm />);

  await submit("ada@", "long enough password");

  expect(screen.getByLabelText("Email")).toHaveAccessibleDescription("Enter a valid email address.");
  expect(mocks.signUp).not.toHaveBeenCalled();
});

test.each([
  [{ code: "user_already_exists", status: 422, message: "x" }, "An account with this email already exists."],
  [{ code: "weak_password", status: 422, message: "x" }, "Choose a stronger password."],
  [{ status: 429, message: "x" }, "Too many attempts."],
  [{ status: 503, message: "x" }, "Could not reach the sign-up service."],
])("explains the failure %j and keeps the form usable", async (error, expected) => {
  mocks.signUp.mockResolvedValue({ data: { session: null }, error });
  render(<RegisterForm />);

  await submit("ada@example.com", "long enough password");

  expect(await screen.findByRole("alert")).toHaveTextContent(expected);
  expect(screen.getByRole("button", { name: "Create account" })).toBeEnabled();
});

test("shows a loading state while the account is created", async () => {
  let finish: (value: unknown) => void = () => {};
  mocks.signUp.mockReturnValue(new Promise((resolve) => (finish = resolve)));
  render(<RegisterForm />);

  await submit("ada@example.com", "long enough password");

  expect(screen.getByRole("button", { name: "Creating account…" })).toBeDisabled();

  finish({ data: { session: null }, error: null });
  expect(await screen.findByRole("status")).toBeInTheDocument();
});
