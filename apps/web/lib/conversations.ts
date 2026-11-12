import { z } from "zod";

import { apiFetch } from "./api";

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

export const messageSchema = z.object({
  id: z.uuid(),
  conversation_id: z.uuid(),
  // The API only ever stores these two roles; anything else is rejected, not rendered.
  role: z.enum(["user", "assistant"]),
  content: z.string(),
  sequence_number: z.number(),
  metadata: z.record(z.string(), z.unknown()).nullable().optional(),
  created_at: z.string(),
});
export type Message = z.infer<typeof messageSchema>;

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
