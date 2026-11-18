import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError, configureApiAuth } from "./api";
import { readEvents, requestCancel, streamQuestion, wasStopped } from "./chat-stream";

const CONVERSATION = "11111111-1111-4111-8111-111111111111";
const QUESTION_ID = "00000000-0000-4000-8000-000000000001";
const ANSWER_ID = "00000000-0000-4000-8000-000000000002";

function message(id: string, role: "user" | "assistant", content: string, sequence: number) {
  return {
    id,
    conversation_id: CONVERSATION,
    role,
    content,
    sequence_number: sequence,
    metadata: role === "assistant" ? { grounded: true } : null,
    model: role === "assistant" ? "gpt-4.1-mini" : null,
    citations:
      role === "assistant"
        ? [
            {
              number: 1,
              document_id: "33333333-3333-4333-8333-333333333333",
              document_name: "policy.pdf",
              chunk_id: "44444444-4444-4444-8444-444444444444",
              page_number: 4,
              paragraph_index: null,
              line_start: null,
              line_end: null,
              excerpt: "within 30 days",
            },
          ]
        : [],
    created_at: "2026-11-12T09:00:00Z",
  };
}

const question = message(QUESTION_ID, "user", "What is the refund policy?", 1);
const reply = message(ANSWER_ID, "assistant", "Refunds within 30 days [1].", 2);

function sse(event: string, data: unknown, id = 1): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const STARTED = sse("message_started", {
  conversation_id: CONVERSATION,
  user_message: question,
  assistant_message_id: ANSWER_ID,
  replayed: false,
});
const COMPLETED =
  sse("citations", { citations: reply.citations }) +
  sse("message_completed", {
    assistant_message: reply,
    model: "gpt-4.1-mini",
    input_tokens: 100,
    output_tokens: 20,
    replayed: false,
  });

/** A response whose body arrives in the given pieces, optionally failing after them. */
function streamed(pieces: string[], { fail = false, status = 200 } = {}): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const piece of pieces) controller.enqueue(encoder.encode(piece));
      if (fail) controller.error(new TypeError("network error"));
      else controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(respond: (init: RequestInit) => Response | Promise<Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return respond(init);
  });
  return calls;
}

async function rejection(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => {
      throw new Error("expected a rejection");
    },
    (reason: unknown) => reason,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  configureApiAuth(null);
});

describe("reading Server-Sent Events", () => {
  async function read(pieces: string[]) {
    const events = [];
    for await (const event of readEvents(streamed(pieces).body!)) events.push(event);
    return events;
  }

  test("events split anywhere across chunks are put back together", async () => {
    const text = sse("text_delta", { text: "Hello" }) + sse("text_delta", { text: " world" });
    for (const size of [1, 2, 5, 13]) {
      const pieces = Array.from({ length: Math.ceil(text.length / size) }, (_, i) =>
        text.slice(i * size, i * size + size),
      );
      expect(await read(pieces)).toEqual([
        { event: "text_delta", data: '{"text":"Hello"}' },
        { event: "text_delta", data: '{"text":" world"}' },
      ]);
    }
  });

  test("handles CRLF line endings, comments and multi-line data", async () => {
    expect(await read([": keep-alive\r\n\r\nevent: note\r\ndata: one\r\ndata: two\r\n\r\n"])).toEqual([
      { event: "note", data: "one\ntwo" },
    ]);
  });

  test("multi-byte characters split between chunks are decoded correctly", async () => {
    const bytes = new TextEncoder().encode(sse("text_delta", { text: "café 😀" }));
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
        controller.close();
      },
    });
    const events = [];
    for await (const event of readEvents(body)) events.push(event);
    expect(JSON.parse(events[0]!.data)).toEqual({ text: "café 😀" });
  });
});

describe("streaming a question", () => {
  test("posts the question with its ID and the session token, and reports each stage", async () => {
    configureApiAuth({ getAccessToken: async () => "token-123", onUnauthorized: vi.fn() });
    const calls = stubFetch(() =>
      streamed([
        STARTED,
        sse("text_delta", { text: "Refunds " }),
        sse("text_delta", { text: "within 30 days." }),
        COMPLETED,
      ]),
    );
    const seen: string[] = [];

    const result = await streamQuestion(CONVERSATION, "What is the refund policy?", QUESTION_ID, {
      onStarted: (started) => seen.push(`started ${started.assistant_message_id}`),
      onDelta: (text) => seen.push(`delta ${text}`),
      onCitations: (citations) => seen.push(`citations ${citations.length}`),
    });

    expect(calls[0]!.url).toBe(`http://localhost:8000/api/v1/conversations/${CONVERSATION}/chat/stream`);
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.headers).toMatchObject({
      Accept: "text/event-stream",
      Authorization: "Bearer token-123",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({
      message: "What is the refund policy?",
      client_message_id: QUESTION_ID,
    });
    expect(seen).toEqual([`started ${ANSWER_ID}`, "delta Refunds ", "delta within 30 days.", "citations 1"]);
    expect(result).toEqual({ user_message: question, assistant_message: reply, replayed: false });
  });

  test("an error event rejects with the server's code and message", async () => {
    stubFetch(() =>
      streamed([
        STARTED,
        sse("text_delta", { text: "Refu" }),
        sse("error", {
          code: "ai_unavailable",
          message: "The AI service is busy or unreachable. Your question was saved; try again shortly.",
          status: 503,
          question_saved: true,
        }),
      ]),
    );

    const error = (await rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID))) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect([error.status, error.code]).toEqual([503, "ai_unavailable"]);
  });

  test("an error before the stream starts is the usual API error, and 401 ends the session", async () => {
    const onUnauthorized = vi.fn();
    configureApiAuth({ getAccessToken: async () => "expired", onUnauthorized });
    stubFetch(() =>
      Response.json({ error: { code: "invalid_token", message: "Sign in again." } }, { status: 401 }),
    );

    const error = (await rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID))) as ApiError;

    expect([error.status, error.code]).toEqual([401, "invalid_token"]);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  test("a busy conversation is reported as such", async () => {
    stubFetch(() =>
      Response.json({ error: { code: "conversation_busy", message: "busy" } }, { status: 409 }),
    );

    const error = (await rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID))) as ApiError;

    expect([error.status, error.code]).toEqual([409, "conversation_busy"]);
  });

  test("no connection at all is a network error", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });

    const error = (await rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID))) as ApiError;

    expect([error.status, error.code]).toEqual([0, "network_error"]);
  });

  test.each([
    ["the connection breaks", { fail: true }],
    ["the stream ends early", { fail: false }],
  ])("when %s, the answer is reported as interrupted", async (_name, options) => {
    stubFetch(() => streamed([STARTED, sse("text_delta", { text: "Refunds" })], options));

    const error = (await rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID))) as ApiError;

    expect([error.status, error.code]).toEqual([0, "stream_interrupted"]);
  });

  test("an event that does not match the protocol is an invalid response", async () => {
    stubFetch(() => streamed([STARTED, sse("message_completed", { assistant_message: { id: "nope" } })]));

    const error = (await rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID))) as ApiError;

    expect(error.code).toBe("invalid_response");
  });

  test("events this client does not know are ignored", async () => {
    stubFetch(() => streamed([STARTED, sse("progress", { stage: "retrieving" }), COMPLETED]));

    expect((await streamQuestion(CONVERSATION, "q", QUESTION_ID)).assistant_message).toEqual(reply);
  });

  test("a non-stream success response is rejected rather than misread", async () => {
    stubFetch(() => Response.json({ ok: true }));

    const error = (await rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID))) as ApiError;

    expect(error.code).toBe("invalid_response");
  });

  test("stopping aborts the request and is told apart from failures", async () => {
    const abort = new AbortController();
    stubFetch(
      (init) =>
        new Promise<Response>((_resolve, reject) => {
          // As fetch does: an already-aborted signal rejects at once.
          if (init.signal?.aborted) reject(new DOMException("aborted", "AbortError"));
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );

    const pending = rejection(streamQuestion(CONVERSATION, "q", QUESTION_ID, {}, abort.signal));
    abort.abort();
    const error = await pending;

    expect(wasStopped(error)).toBe(true);
    expect(wasStopped(new ApiError(0, "network_error", "x"))).toBe(false);
  });
});

describe("asking to cancel", () => {
  test("posts to the conversation's cancel endpoint with the session token", async () => {
    configureApiAuth({ getAccessToken: async () => "token-123", onUnauthorized: vi.fn() });
    const calls = stubFetch(() => Response.json({ cancel_requested: true }, { status: 202 }));

    await requestCancel(CONVERSATION);

    expect(calls[0]!.url).toBe(`http://localhost:8000/api/v1/conversations/${CONVERSATION}/chat/cancel`);
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.headers).toMatchObject({ Authorization: "Bearer token-123" });
  });

  test("never rejects, and never holds Stop up for long", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(requestCancel(CONVERSATION)).resolves.toBeUndefined();

    vi.stubGlobal(
      "fetch",
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) =>
          init.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
        ),
    );
    const started = Date.now();
    await requestCancel(CONVERSATION, 50);
    expect(Date.now() - started).toBeLessThan(1000);
  });
});
