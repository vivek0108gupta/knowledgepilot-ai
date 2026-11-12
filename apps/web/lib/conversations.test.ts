import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError, configureApiAuth } from "./api";
import {
  askQuestion,
  chatErrorMessage,
  chatPath,
  conversationKeys,
  conversationTitleSchema,
  createConversation,
  listConversations,
  listMessages,
  MAX_MESSAGE_LENGTH,
  messageSchema,
  questionWasKept,
  renameConversation,
  sendMessage,
  validateMessage,
} from "./conversations";

const WORKSPACE = "8a5c1f0e-0000-4000-8000-000000000000";
const CONVERSATION = "33333333-3333-4333-8333-333333333333";
const CLIENT_ID = "44444444-4444-4444-8444-444444444444";

const conversation = {
  id: CONVERSATION,
  workspace_id: WORKSPACE,
  title: "New conversation",
  status: "active",
  message_count: 0,
  created_at: "2026-11-11T00:00:00Z",
  updated_at: "2026-11-11T00:00:00Z",
};

const message = {
  id: "55555555-5555-4555-8555-555555555555",
  conversation_id: CONVERSATION,
  role: "user",
  content: "What is the refund policy?",
  sequence_number: 1,
  metadata: null,
  created_at: "2026-11-11T00:00:00Z",
};

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(body: unknown, status = 200): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return status === 204 ? new Response(null, { status }) : Response.json(body, { status });
  });
  return calls;
}

const bodyOf = (call: Call) => JSON.parse(String(call.init.body));

afterEach(() => {
  vi.unstubAllGlobals();
  configureApiAuth(null);
});

describe("requests", () => {
  test("creating a conversation sends no owner, workspace or status: the server decides those", async () => {
    const calls = stubFetch(conversation, 201);

    await createConversation(WORKSPACE);

    expect(calls[0]!.url).toBe(`http://localhost:8000/api/v1/workspaces/${WORKSPACE}/conversations`);
    expect(calls[0]!.init.method).toBe("POST");
    expect(bodyOf(calls[0]!)).toEqual({});
  });

  test("sending a message sends only its text and an idempotency ID, never a role", async () => {
    const calls = stubFetch(message, 201);
    configureApiAuth({ getAccessToken: async () => "token-123", onUnauthorized: vi.fn() });

    const stored = await sendMessage(CONVERSATION, "What is the refund policy?", CLIENT_ID);

    expect(stored).toEqual({ ...message, citations: [] });
    expect(calls[0]!.url).toBe(`http://localhost:8000/api/v1/conversations/${CONVERSATION}/messages`);
    expect(bodyOf(calls[0]!)).toEqual({
      content: "What is the refund policy?",
      client_message_id: CLIENT_ID,
    });
    expect(Object.keys(bodyOf(calls[0]!))).not.toContain("role");
    expect((calls[0]!.init.headers as Record<string, string>).Authorization).toBe("Bearer token-123");
  });

  test("renaming sends only the title", async () => {
    const calls = stubFetch({ ...conversation, title: "Refunds" });

    await renameConversation(CONVERSATION, "Refunds");

    expect(calls[0]!.init.method).toBe("PATCH");
    expect(bodyOf(calls[0]!)).toEqual({ title: "Refunds" });
  });

  test("the list is requested for one workspace", async () => {
    const calls = stubFetch({ items: [conversation], total: 1, limit: 100, offset: 0 });

    const page = await listConversations(WORKSPACE);

    expect(calls[0]!.url).toBe(
      `http://localhost:8000/api/v1/workspaces/${WORKSPACE}/conversations?limit=100&offset=0`,
    );
    expect(page.items).toEqual([conversation]);
  });

  test("history asks for the latest page, or the page before a given message", async () => {
    const calls = stubFetch({ items: [message], total: 1, has_more: false });

    await listMessages(CONVERSATION);
    await listMessages(CONVERSATION, 51);

    expect(calls[0]!.url).toMatch(/\/messages\?limit=50$/);
    expect(calls[1]!.url).toMatch(/\/messages\?limit=50&before=51$/);
  });
});

describe("response checking", () => {
  test("a message with a role other than user or assistant is rejected, not rendered", () => {
    expect(messageSchema.safeParse(message).success).toBe(true);
    expect(messageSchema.safeParse({ ...message, role: "assistant" }).success).toBe(true);
    expect(messageSchema.safeParse({ ...message, role: "system" }).success).toBe(false);
  });

  test("a history response containing a system message fails as a whole", async () => {
    stubFetch({ items: [{ ...message, role: "system" }], total: 1, has_more: false });

    await expect(listMessages(CONVERSATION)).rejects.toMatchObject({ code: "invalid_response" });
  });
});

describe("message validation", () => {
  test("accepts ordinary text and keeps multi-line drafts valid", () => {
    expect(validateMessage("What is the refund policy?")).toBeNull();
    expect(validateMessage("line one\nline two")).toBeNull();
  });

  test.each(["", "   ", "\n\n\t"])("rejects a blank draft %j", (draft) => {
    expect(validateMessage(draft)).toBe("Write a message first.");
  });

  test("accepts the longest allowed message and rejects one character more", () => {
    expect(validateMessage("a".repeat(MAX_MESSAGE_LENGTH))).toBeNull();
    expect(validateMessage("a".repeat(MAX_MESSAGE_LENGTH + 1))).toBe(
      "Messages can be up to 8,000 characters. This one is 8,001.",
    );
  });
});

describe("titles", () => {
  test("are trimmed and limited to 120 characters", () => {
    expect(conversationTitleSchema.parse("  Refunds  ")).toBe("Refunds");
    expect(conversationTitleSchema.safeParse("   ").success).toBe(false);
    expect(conversationTitleSchema.safeParse("x".repeat(120)).success).toBe(true);
    expect(conversationTitleSchema.safeParse("x".repeat(121)).success).toBe(false);
  });
});

describe("paths and cache keys", () => {
  test("chat URLs carry the workspace, and the conversation when one is open", () => {
    expect(chatPath(WORKSPACE)).toBe(`/dashboard/workspaces/${WORKSPACE}/chat`);
    expect(chatPath(WORKSPACE, CONVERSATION)).toBe(`/dashboard/workspaces/${WORKSPACE}/chat/${CONVERSATION}`);
  });

  test("every cache key starts with the workspace, so workspaces never share cached chat data", () => {
    for (const key of [
      conversationKeys.list(WORKSPACE),
      conversationKeys.detail(WORKSPACE, CONVERSATION),
      conversationKeys.messages(WORKSPACE, CONVERSATION),
    ]) {
      expect(key.slice(0, 2)).toEqual(["workspaces", WORKSPACE]);
    }
  });
});

describe("asking", () => {
  const answered = {
    conversation_id: CONVERSATION,
    user_message: message,
    assistant_message: {
      ...message,
      id: "99999999-9999-4999-8999-999999999999",
      role: "assistant",
      content: "Refunds within 30 days [1].",
      sequence_number: 2,
      metadata: { grounded: true },
      model: "gpt-4.1-mini",
      citations: [
        {
          number: 1,
          document_id: WORKSPACE,
          document_name: "refund-policy.pdf",
          chunk_id: CLIENT_ID,
          page_number: 4,
          paragraph_index: null,
          line_start: null,
          line_end: null,
          excerpt: "within 30 days",
        },
      ],
    },
  };

  test("asks through the chat endpoint with only the question and an idempotency ID", async () => {
    const calls = stubFetch(answered, 201);

    const result = await askQuestion(CONVERSATION, "What is the refund policy?", CLIENT_ID);

    expect(calls[0]!.url).toBe(`http://localhost:8000/api/v1/conversations/${CONVERSATION}/chat`);
    expect(calls[0]!.init.method).toBe("POST");
    expect(bodyOf(calls[0]!)).toEqual({
      message: "What is the refund policy?",
      client_message_id: CLIENT_ID,
    });
    expect(result.assistant_message.citations[0]!.page_number).toBe(4);
  });

  test("an answer whose citation is malformed is rejected, not rendered", async () => {
    stubFetch(
      { ...answered, assistant_message: { ...answered.assistant_message, citations: [{ number: 1 }] } },
      201,
    );

    await expect(askQuestion(CONVERSATION, "q", CLIENT_ID)).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  test.each([
    [new ApiError(503, "ai_unavailable", "Saved; try again."), true],
    [new ApiError(502, "ai_invalid_response", "x"), true],
    [new ApiError(502, "ai_answer_too_long", "x"), true],
    [new ApiError(500, "answer_failed", "x"), true],
    [new ApiError(409, "answer_superseded", "x"), true],
    [new ApiError(0, "stream_interrupted", "x"), true],
    [new ApiError(503, "embeddings_unavailable", "x"), true],
    [new ApiError(500, "answer_not_saved", "x"), true],
    [new ApiError(409, "answer_in_progress", "x"), true],
    [new ApiError(0, "network_error", "x"), true],
    [new ApiError(409, "conversation_busy", "x"), false],
    [new ApiError(422, "validation_error", "x"), false],
    [new ApiError(404, "conversation_not_found", "x"), false],
    [new ApiError(401, "token_expired", "x"), false],
    [new ApiError(503, "ai_not_configured", "x"), false],
  ])("%s: question kept = %s", (error, kept) => {
    expect(questionWasKept(error)).toBe(kept);
  });

  test("error messages say what to do, and never pass on unexpected server text", () => {
    expect(
      chatErrorMessage(new ApiError(503, "ai_unavailable", "Your question was saved; try again shortly.")),
    ).toBe("Your question was saved; try again shortly.");
    expect(chatErrorMessage(new ApiError(401, "token_expired", "x"))).toMatch(/Sign in again/);
    expect(chatErrorMessage(new ApiError(500, "internal_error", "Traceback db:5432"))).toBe(
      "Something went wrong. Please try again.",
    );
    expect(chatErrorMessage(new TypeError("boom"))).toBe("Something went wrong. Please try again.");
  });
});
