import { afterEach, expect, test, vi } from "vitest";
import { z } from "zod";

import { ApiError, apiFetch, configureApiAuth, getHealth, healthSchema, readinessSchema } from "./api";

interface Call {
  url: string;
  init: RequestInit;
}

/** Replaces `fetch` for one test and records what it was called with. */
function stubFetch(respond: () => Response | Promise<Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return respond();
  });
  return calls;
}

const headersOf = (call: Call) => call.init.headers as Record<string, string>;

async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => null,
    (reason: unknown) => reason,
  );
  expect(error).toBeInstanceOf(ApiError);
  return error as ApiError;
}

afterEach(() => {
  vi.unstubAllGlobals();
  configureApiAuth(null);
});

test("returns the JSON body and calls the versioned URL", async () => {
  const calls = stubFetch(() => Response.json({ status: "ok" }));

  expect(await getHealth()).toEqual({ status: "ok" });
  expect(calls.map((call) => call.url)).toEqual(["http://localhost:8000/api/v1/health"]);
});

test("turns the API's error envelope into an ApiError", async () => {
  stubFetch(() =>
    Response.json(
      { error: { code: "database_unavailable", message: "Database is unreachable.", request_id: "abc123" } },
      { status: 503 },
    ),
  );

  const error = await rejection(apiFetch("/health/ready", readinessSchema));

  expect(error).toMatchObject({
    status: 503,
    code: "database_unavailable",
    message: "Database is unreachable.",
    requestId: "abc123",
  });
});

test("handles an error response that is not JSON", async () => {
  stubFetch(
    () => new Response("<html>Bad Gateway</html>", { status: 502, headers: { "X-Request-ID": "proxy-1" } }),
  );

  const error = await rejection(apiFetch("/health", healthSchema));

  expect(error).toMatchObject({ status: 502, code: "unexpected_error", requestId: "proxy-1" });
});

test("reports a network failure as status 0", async () => {
  stubFetch(() => {
    throw new TypeError("fetch failed");
  });

  const error = await rejection(apiFetch("/health", healthSchema));

  expect(error).toMatchObject({ status: 0, code: "network_error" });
});

test("lets an abort through unchanged, so callers can ignore it", async () => {
  const controller = new AbortController();
  const abort = new DOMException("Aborted", "AbortError");
  stubFetch(() => {
    controller.abort();
    throw abort;
  });

  await expect(apiFetch("/health", healthSchema, { signal: controller.signal })).rejects.toBe(abort);
});

test("rejects a success response that does not match the schema", async () => {
  stubFetch(() => Response.json({ status: "ok" }));

  const error = await rejection(apiFetch("/health/ready", readinessSchema));

  expect(error).toMatchObject({ status: 200, code: "invalid_response" });
});

test("sends no Authorization header when signed out", async () => {
  const calls = stubFetch(() => Response.json({ status: "ok" }));
  configureApiAuth({ getAccessToken: async () => null, onUnauthorized: vi.fn() });

  await getHealth();

  expect(headersOf(calls[0]!)).not.toHaveProperty("Authorization");
});

test("sends the session's access token as a bearer token", async () => {
  const calls = stubFetch(() => Response.json({ status: "ok" }));
  configureApiAuth({ getAccessToken: async () => "token-123", onUnauthorized: vi.fn() });

  await getHealth();

  expect(headersOf(calls[0]!).Authorization).toBe("Bearer token-123");
});

test("asks for a fresh token on every request, so a refreshed one is picked up", async () => {
  const calls = stubFetch(() => Response.json({ status: "ok" }));
  const tokens = ["first", "second"];
  configureApiAuth({ getAccessToken: async () => tokens.shift() ?? null, onUnauthorized: vi.fn() });

  await getHealth();
  await getHealth();

  expect(calls.map((call) => headersOf(call).Authorization)).toEqual(["Bearer first", "Bearer second"]);
});

test("reports a 401 so the app can end the session", async () => {
  stubFetch(() =>
    Response.json(
      { error: { code: "token_expired", message: "The access token has expired." } },
      { status: 401 },
    ),
  );
  const onUnauthorized = vi.fn();
  configureApiAuth({ getAccessToken: async () => "stale", onUnauthorized });

  const error = await rejection(apiFetch("/workspaces", z.array(z.unknown())));

  expect(error.code).toBe("token_expired");
  expect(onUnauthorized).toHaveBeenCalledTimes(1);
});

test("does not end the session for a 403", async () => {
  stubFetch(() => Response.json({ error: { code: "forbidden", message: "No." } }, { status: 403 }));
  const onUnauthorized = vi.fn();
  configureApiAuth({ getAccessToken: async () => "valid", onUnauthorized });

  await rejection(apiFetch("/workspaces", z.array(z.unknown())));

  expect(onUnauthorized).not.toHaveBeenCalled();
});

test("sends a JSON body with the chosen method", async () => {
  const calls = stubFetch(() => Response.json({ ok: true }, { status: 201 }));

  await apiFetch("/workspaces", z.object({ ok: z.boolean() }), {
    method: "POST",
    body: { name: "Research" },
  });

  expect(calls[0]!.init.method).toBe("POST");
  expect(calls[0]!.init.body).toBe('{"name":"Research"}');
  expect(headersOf(calls[0]!)["Content-Type"]).toBe("application/json");
});

test("accepts an empty 204 response", async () => {
  stubFetch(() => new Response(null, { status: 204 }));

  await expect(apiFetch("/workspaces/1", z.void(), { method: "DELETE" })).resolves.toBeUndefined();
});
