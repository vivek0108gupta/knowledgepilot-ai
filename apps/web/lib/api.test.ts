import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { ApiError, apiFetch, getHealth } from "./api.ts";

const realFetch = globalThis.fetch;

/** Replaces `fetch` for one test and records the URLs it was called with. */
function stubFetch(respond: () => Response | Promise<Response>): string[] {
  const calls: string[] = [];
  globalThis.fetch = async (input) => {
    calls.push(String(input));
    return respond();
  };
  return calls;
}

afterEach(() => {
  globalThis.fetch = realFetch;
});

test("returns the JSON body and calls the versioned URL", async () => {
  const calls = stubFetch(() => Response.json({ status: "ok" }));

  assert.deepEqual(await getHealth(), { status: "ok" });
  assert.deepEqual(calls, ["http://localhost:8000/api/v1/health"]);
});

test("turns the API's error envelope into an ApiError", async () => {
  stubFetch(() =>
    Response.json(
      { error: { code: "database_unavailable", message: "Database is unreachable.", request_id: "abc123" } },
      { status: 503 },
    ),
  );

  await assert.rejects(apiFetch("/health/ready"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 503);
    assert.equal(error.code, "database_unavailable");
    assert.equal(error.message, "Database is unreachable.");
    assert.equal(error.requestId, "abc123");
    return true;
  });
});

test("handles an error response that is not JSON", async () => {
  stubFetch(() => new Response("<html>Bad Gateway</html>", { status: 502, headers: { "X-Request-ID": "proxy-1" } }));

  await assert.rejects(apiFetch("/health"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 502);
    assert.equal(error.code, "unexpected_error");
    assert.equal(error.requestId, "proxy-1");
    return true;
  });
});

test("reports a network failure as status 0", async () => {
  stubFetch(() => {
    throw new TypeError("fetch failed");
  });

  await assert.rejects(apiFetch("/health"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 0);
    assert.equal(error.code, "network_error");
    return true;
  });
});

test("lets an abort through unchanged, so callers can ignore it", async () => {
  const controller = new AbortController();
  const abort = new DOMException("Aborted", "AbortError");
  stubFetch(() => {
    controller.abort();
    throw abort;
  });

  await assert.rejects(apiFetch("/health", { signal: controller.signal }), (error) => error === abort);
});
