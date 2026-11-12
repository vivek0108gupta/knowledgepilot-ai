import { z } from "zod";

import { API_V1, ApiError, apiErrorFrom, authHeaders } from "./api";
import { citationSchema, messageSchema, type Citation, type Message } from "./conversations";

/*
 * Streaming answers over Server-Sent Events, from POST /conversations/{id}/chat/stream.
 *
 * The browser's EventSource cannot send a POST body or an Authorization header, so the stream
 * is read with fetch. The access token is taken from the session for each request, as for
 * every other call; nothing is stored.
 *
 * Text arrives provisional, without source markers. The answer that counts is the checked one
 * in `message_completed`, which replaces it.
 */

const startedSchema = z.object({
  conversation_id: z.uuid(),
  user_message: messageSchema,
  assistant_message_id: z.uuid(),
  replayed: z.boolean(),
});
export type StreamStarted = z.infer<typeof startedSchema>;

const deltaSchema = z.object({ text: z.string() });
const citationsSchema = z.object({ citations: z.array(citationSchema) });
const completedSchema = z.object({
  assistant_message: messageSchema,
  model: z.string().nullable(),
  input_tokens: z.number().nullable(),
  output_tokens: z.number().nullable(),
  replayed: z.boolean(),
});
const errorSchema = z.object({
  code: z.string(),
  message: z.string(),
  status: z.number(),
  question_saved: z.boolean(),
});

export interface StreamedAnswer {
  user_message: Message;
  assistant_message: Message;
  replayed: boolean;
}

export interface StreamHandlers {
  /** The question is stored; its answer will have `assistantMessageId`. */
  onStarted?: (started: StreamStarted) => void;
  /** More provisional text, to append to what was shown. */
  onDelta?: (text: string) => void;
  /** The checked citations, just before the checked answer. */
  onCitations?: (citations: Citation[]) => void;
}

interface ServerSentEvent {
  event: string;
  data: string;
}

/** Splits a byte stream into Server-Sent Events, per the HTML specification's parsing rules. */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ServerSentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let data: string[] = [];
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      let newline: number;
      while ((newline = buffer.search(/\r\n|\r|\n/)) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + (buffer.startsWith("\r\n", newline) ? 2 : 1));
        if (line === "") {
          if (data.length > 0) yield { event, data: data.join("\n") };
          event = "message";
          data = [];
          continue;
        }
        if (line.startsWith(":")) continue; // a comment, such as a keep-alive
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parse<T>(schema: z.ZodType<T>, data: string): T {
  let json: unknown;
  try {
    json = JSON.parse(data);
  } catch {
    json = undefined;
  }
  const parsed = schema.safeParse(json);
  if (!parsed.success) throw new ApiError(200, "invalid_response", "The server sent an unexpected response.");
  return parsed.data;
}

const interrupted = () =>
  new ApiError(0, "stream_interrupted", "The connection was lost before the answer was finished.");

/**
 * Asks a question and streams the answer. Resolves with the stored question and the checked,
 * stored answer. Rejects with an `ApiError` (status 0 when the connection failed or broke),
 * or with the abort error when `signal` is aborted, which also abandons the answer.
 *
 * Reusing `clientMessageId` after a failure answers the stored question without storing it
 * twice; once answered, the same ID returns the stored answer.
 */
export async function streamQuestion(
  conversationId: string,
  message: string,
  clientMessageId: string,
  handlers: StreamHandlers = {},
  signal?: AbortSignal,
): Promise<StreamedAnswer> {
  let response: Response;
  try {
    response = await fetch(`${API_V1}/conversations/${conversationId}/chat/stream`, {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        ...(await authHeaders()),
      },
      body: JSON.stringify({ message, client_message_id: clientMessageId }),
      signal,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    throw new ApiError(0, "network_error", "Could not reach the server.");
  }

  if (!response.ok) throw await apiErrorFrom(response);
  if (!response.body || !response.headers.get("Content-Type")?.startsWith("text/event-stream")) {
    throw new ApiError(response.status, "invalid_response", "The server sent an unexpected response.");
  }

  let userMessage: Message | null = null;
  try {
    for await (const { event, data } of readEvents(response.body)) {
      if (event === "message_started") {
        const started = parse(startedSchema, data);
        userMessage = started.user_message;
        handlers.onStarted?.(started);
      } else if (event === "text_delta") {
        handlers.onDelta?.(parse(deltaSchema, data).text);
      } else if (event === "citations") {
        handlers.onCitations?.(parse(citationsSchema, data).citations);
      } else if (event === "message_completed") {
        const completed = parse(completedSchema, data);
        if (!userMessage)
          throw new ApiError(200, "invalid_response", "The server sent an unexpected response.");
        return {
          user_message: userMessage,
          assistant_message: completed.assistant_message,
          replayed: completed.replayed,
        };
      } else if (event === "error") {
        const failure = parse(errorSchema, data);
        throw new ApiError(failure.status, failure.code, failure.message);
      }
      // Events this client does not know are ignored, so the protocol can grow.
    }
  } catch (cause) {
    if (signal?.aborted) throw cause;
    if (cause instanceof ApiError) throw cause;
    throw interrupted();
  }
  if (signal?.aborted) throw new DOMException("The answer was stopped.", "AbortError");
  throw interrupted();
}

/** Whether a rejection from `streamQuestion` means the person stopped the answer. */
export function wasStopped(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
