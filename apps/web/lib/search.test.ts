import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError, configureApiAuth } from "./api";
import {
  isRetryable,
  listSearchableDocuments,
  searchErrorMessage,
  searchWorkspace,
  similarityPercent,
  sourceLabel,
  validateQuery,
} from "./search";

const WORKSPACE = "8a5c1f0e-0000-4000-8000-000000000000";
const DOCUMENT = "33333333-3333-4333-8333-333333333333";

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(body: unknown, status = 200): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return Response.json(body, { status });
  });
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  configureApiAuth(null);
});

describe("search request", () => {
  const response = {
    query: "refund policy",
    results: [
      {
        chunk_id: "44444444-4444-4444-8444-444444444444",
        document_id: DOCUMENT,
        document_name: "refund-policy.pdf",
        content: "Customers can request a refund within 30 days.",
        similarity: 0.87,
        source: { page_number: 4, paragraph_index: null, line_start: null, line_end: null },
      },
    ],
  };

  test("posts the query to the workspace named in the URL, with the session token", async () => {
    const calls = stubFetch(response);
    configureApiAuth({ getAccessToken: async () => "token-123", onUnauthorized: vi.fn() });

    const result = await searchWorkspace(WORKSPACE, { query: "refund policy" });

    expect(result).toEqual(response);
    expect(calls[0]!.url).toBe(`http://localhost:8000/api/v1/workspaces/${WORKSPACE}/search`);
    expect(calls[0]!.init.method).toBe("POST");
    expect((calls[0]!.init.headers as Record<string, string>).Authorization).toBe("Bearer token-123");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      query: "refund policy",
      top_k: 5,
      document_ids: [],
    });
  });

  test("the body carries no workspace or user ID: the API takes both from the URL and the token", async () => {
    const calls = stubFetch(response);

    await searchWorkspace(WORKSPACE, { query: "refund policy", documentIds: [DOCUMENT], topK: 3 });

    const body = JSON.parse(String(calls[0]!.init.body));
    expect(Object.keys(body).sort()).toEqual(["document_ids", "query", "top_k"]);
    expect(body).toMatchObject({ document_ids: [DOCUMENT], top_k: 3 });
  });

  test("a response of the wrong shape is rejected rather than rendered", async () => {
    stubFetch({ query: "x", results: [{ document_name: "a.pdf" }] });

    await expect(searchWorkspace(WORKSPACE, { query: "x" })).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  test("the document filter lists only ready documents of this workspace", async () => {
    const calls = stubFetch({ items: [], total: 0, limit: 100, offset: 0 });

    await listSearchableDocuments(WORKSPACE);

    expect(calls[0]!.url).toBe(
      `http://localhost:8000/api/v1/workspaces/${WORKSPACE}/documents?status=ready&limit=100&offset=0`,
    );
  });
});

describe("source references", () => {
  const none = { page_number: null, paragraph_index: null, line_start: null, line_end: null };

  test("a PDF passage is cited by page", () => {
    expect(sourceLabel({ ...none, page_number: 4 })).toBe("Page 4");
  });

  test("a Word passage is cited by paragraph, counted from one, and never by page", () => {
    expect(sourceLabel({ ...none, paragraph_index: 0 })).toBe("Paragraph 1");
    expect(sourceLabel({ ...none, paragraph_index: 12 })).toBe("Paragraph 13");
    expect(sourceLabel({ ...none, paragraph_index: 12 })).not.toMatch(/page/i);
  });

  test("a text passage is cited by line range", () => {
    expect(sourceLabel({ ...none, line_start: 10, line_end: 24 })).toBe("Lines 10–24");
    expect(sourceLabel({ ...none, line_start: 7, line_end: 7 })).toBe("Line 7");
    expect(sourceLabel({ ...none, line_start: 7 })).toBe("Line 7");
  });

  test("no reference gives no label, rather than an invented one", () => {
    expect(sourceLabel(none)).toBeNull();
  });
});

describe("similarity", () => {
  test("is shown as a whole percentage, kept between 0 and 100", () => {
    expect(similarityPercent(0.8749)).toBe(87);
    expect(similarityPercent(0.875)).toBe(88);
    expect(similarityPercent(1)).toBe(100);
    expect(similarityPercent(1.0000001)).toBe(100);
    expect(similarityPercent(0)).toBe(0);
    expect(similarityPercent(-0.3)).toBe(0);
  });
});

describe("query validation", () => {
  test("accepts an ordinary question", () => {
    expect(validateQuery("What is the refund policy?")).toBeNull();
  });

  test.each(["", "   ", "\n\t"])("rejects a blank query %j", (query) => {
    expect(validateQuery(query)).toBe("Enter something to search for.");
  });

  test("accepts 1,000 characters and rejects 1,001", () => {
    expect(validateQuery("a".repeat(1000))).toBeNull();
    expect(validateQuery("a".repeat(1001))).toBe("Use 1,000 characters or fewer.");
  });

  test("measures the query after trimming", () => {
    expect(validateQuery(`  ${"a".repeat(1000)}  `)).toBeNull();
  });
});

describe("error messages", () => {
  test.each([
    [new ApiError(401, "token_expired", "x"), "Your session has expired. Sign in again to search.", false],
    [new ApiError(403, "forbidden", "x"), "You do not have permission to search this workspace.", false],
    [
      new ApiError(404, "workspace_not_found", "x"),
      "This workspace no longer exists, or you are no longer a member of it.",
      false,
    ],
    [
      new ApiError(422, "validation_error", "x"),
      "That search could not be run. Shorten or rephrase it and try again.",
      false,
    ],
    [
      new ApiError(429, "too_many_requests", "x"),
      "Too many searches in a short time. Wait a moment and try again.",
      true,
    ],
    [
      new ApiError(500, "internal_error", "x"),
      "Search is temporarily unavailable. Try again in a moment.",
      true,
    ],
    [
      new ApiError(503, "embeddings_unavailable", "x"),
      "Search is temporarily unavailable. Try again in a moment.",
      true,
    ],
    [
      new ApiError(503, "embeddings_not_configured", "x"),
      "Search has not been set up on the server yet.",
      false,
    ],
    [
      new ApiError(0, "network_error", "x"),
      "Could not reach the server. Check your connection and try again.",
      true,
    ],
  ])("%s", (error, message, retryable) => {
    expect(searchErrorMessage(error)).toBe(message);
    expect(isRetryable(error)).toBe(retryable);
  });

  test("the server's own wording is never shown, so nothing internal can leak through it", () => {
    const error = new ApiError(500, "internal_error", "psycopg2.OperationalError at db-internal:5432");

    expect(searchErrorMessage(error)).not.toContain("psycopg2");
  });

  test("an unknown failure gets a generic message", () => {
    expect(searchErrorMessage(new TypeError("boom"))).toBe("Something went wrong. Please try again.");
  });
});
