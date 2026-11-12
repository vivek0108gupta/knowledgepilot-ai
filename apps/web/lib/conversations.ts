import { z } from "zod";

import { ApiError, apiFetch } from "./api";

export const DEFAULT_CONVERSATION_TITLE = "New conversation";
export const MAX_TITLE_LENGTH = 120;
export const MAX_MESSAGE_LENGTH = 8000;
export const MESSAGE_PAGE_SIZE = 50;

export const conversationSchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  title: z.string(),
  status: z.enum(["active", "archived"]),
  message_count: z.number(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Conversation = z.infer<typeof conversationSchema>;

const conversationPageSchema = z.object({
  items: z.array(conversationSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});
export type ConversationPage = z.infer<typeof conversationPageSchema>;

/** A source an answer relies on. The server builds these from the passages it retrieved. */
export const citationSchema = z.object({
  number: z.number(),
  document_id: z.uuid(),
  document_name: z.string(),
  chunk_id: z.uuid(),
  page_number: z.number().nullable(),
  paragraph_index: z.number().nullable(),
  line_start: z.number().nullable(),
  line_end: z.number().nullable(),
  excerpt: z.string(),
});
export type Citation = z.infer<typeof citationSchema>;

export const messageSchema = z.object({
  id: z.uuid(),
  conversation_id: z.uuid(),
  // The API only ever stores these two roles; anything else is rejected, not rendered.
  role: z.enum(["user", "assistant"]),
  content: z.string(),
  sequence_number: z.number(),
  metadata: z.record(z.string(), z.unknown()).nullable().optional(),
  model: z.string().nullable().optional(),
  citations: z.array(citationSchema).optional().default([]),
  created_at: z.string(),
});
export type Message = z.infer<typeof messageSchema>;

const chatResponseSchema = z.object({
  conversation_id: z.uuid(),
  user_message: messageSchema,
  assistant_message: messageSchema,
});
export type ChatResponse = z.infer<typeof chatResponseSchema>;

const messagePageSchema = z.object({
  items: z.array(messageSchema),
  total: z.number(),
  has_more: z.boolean(),
});
export type MessagePage = z.infer<typeof messagePageSchema>;

/** Query keys in one place. Every key starts with the workspace, so nothing is shared across workspaces. */
export const conversationKeys = {
  list: (workspaceId: string) => ["workspaces", workspaceId, "conversations"] as const,
  detail: (workspaceId: string, conversationId: string) =>
    ["workspaces", workspaceId, "conversations", conversationId] as const,
  messages: (workspaceId: string, conversationId: string) =>
    ["workspaces", workspaceId, "conversations", conversationId, "messages"] as const,
};

export const listConversations = (workspaceId: string, signal?: AbortSignal) =>
  apiFetch(`/workspaces/${workspaceId}/conversations?limit=100&offset=0`, conversationPageSchema, { signal });

export const getConversation = (conversationId: string, signal?: AbortSignal) =>
  apiFetch(`/conversations/${conversationId}`, conversationSchema, { signal });

export const createConversation = (workspaceId: string) =>
  apiFetch(`/workspaces/${workspaceId}/conversations`, conversationSchema, { method: "POST", body: {} });

export const renameConversation = (conversationId: string, title: string) =>
  apiFetch(`/conversations/${conversationId}`, conversationSchema, { method: "PATCH", body: { title } });

export const deleteConversation = (conversationId: string) =>
  apiFetch(`/conversations/${conversationId}`, z.void(), { method: "DELETE" });

/** The most recent messages, oldest first. Pass `before` to load the page preceding it. */
export function listMessages(conversationId: string, before?: number, signal?: AbortSignal) {
  const params = new URLSearchParams({ limit: String(MESSAGE_PAGE_SIZE) });
  if (before !== undefined) params.set("before", String(before));
  return apiFetch(`/conversations/${conversationId}/messages?${params}`, messagePageSchema, { signal });
}

/**
 * Sends a message from the signed-in user. There is no role to send: the API stores every
 * message from this endpoint as the user's. `clientMessageId` makes a retry safe: sending
 * the same ID again returns the message already stored instead of creating another.
 */
export const sendMessage = (conversationId: string, content: string, clientMessageId: string) =>
  apiFetch(`/conversations/${conversationId}/messages`, messageSchema, {
    method: "POST",
    body: { content, client_message_id: clientMessageId },
  });

/**
 * Asks a question in a conversation and returns the stored question and its answer.
 * The workspace is the conversation's own; nothing here can point the search elsewhere.
 * Reusing `clientMessageId` after a failure retries the answer without storing the question
 * twice; once answered, the same ID returns the stored answer.
 */
export const askQuestion = (conversationId: string, message: string, clientMessageId: string) =>
  apiFetch(`/conversations/${conversationId}/chat`, chatResponseSchema, {
    method: "POST",
    body: { message, client_message_id: clientMessageId },
  });

/** Failures after which the question is already stored, so retrying is the right move. */
const SAVED_QUESTION_CODES = new Set([
  "ai_unavailable",
  "ai_misconfigured",
  "ai_rejected",
  "ai_invalid_response",
  "embeddings_unavailable",
  "answer_not_saved",
  "answer_in_progress",
]);

/** Whether a failed question was stored and can be retried with the same ID. A lost
 * connection counts: the question may have been stored, and a retry is idempotent. */
export function questionWasKept(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 0 || SAVED_QUESTION_CODES.has(error.code));
}

/** A message for a failed question that says what the person can do. */
export function chatErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Please try again.";
  if (error.status === 0) return "Could not reach the server. Check your connection, then try again.";
  if (error.status === 401) return "Your session has expired. Sign in again to continue.";
  if (error.status === 404) return "This conversation no longer exists, or it is not one of yours.";
  if (error.code === "conversation_busy") return "An answer is still being written. Wait for it to finish.";
  if (error.code === "answer_in_progress")
    return "The answer to this question is still being written. Try again in a moment.";
  if (error.code === "ai_not_configured") return "Answering questions has not been set up on the server yet.";
  if (error.status === 422) return "That question could not be sent. Shorten or change it and try again.";
  if (error.status === 429) return "Too many requests. Wait a moment and try again.";
  // These messages are written by the API for people, and say the question was kept.
  if (SAVED_QUESTION_CODES.has(error.code) || error.code === "conversation_archived") return error.message;
  return "Something went wrong. Please try again.";
}

export const conversationTitleSchema = z
  .string()
  .trim()
  .min(1, "Enter a title.")
  .max(MAX_TITLE_LENGTH, `Use ${MAX_TITLE_LENGTH} characters or fewer.`);

/** Why a draft cannot be sent, or null if it can. The API applies the same limits. */
export function validateMessage(draft: string): string | null {
  if (!draft.trim()) return "Write a message first.";
  if (draft.length > MAX_MESSAGE_LENGTH) {
    return `Messages can be up to ${MAX_MESSAGE_LENGTH.toLocaleString()} characters. This one is ${draft.length.toLocaleString()}.`;
  }
  return null;
}

export const chatPath = (workspaceId: string, conversationId?: string) =>
  `/dashboard/workspaces/${workspaceId}/chat${conversationId ? `/${conversationId}` : ""}`;
