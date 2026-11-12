import { z } from "zod";

import { apiFetch } from "./api";

/*
 * Usage and quotas. Every number shown comes from the API, which computes it from the usage
 * ledger: the client never decides what remains. Costs arrive as decimal strings and are
 * estimates from the configured price table, not an invoice.
 */

const decimal = z.union([z.string(), z.number()]).transform((value) => Number(value));

const totalsSchema = z.object({ tokens: z.number(), estimated_cost: decimal, operations: z.number() });

export const operationSchema = z.enum(["chat", "chat_stream", "query_embedding", "document_embedding"]);
export type Operation = z.infer<typeof operationSchema>;

export const OPERATION_LABELS: Record<Operation, string> = {
  chat: "Answers",
  chat_stream: "Streamed answers",
  query_embedding: "Question and search embeddings",
  document_embedding: "Document indexing",
};

const workspaceAllowanceSchema = z.object({
  monthly_token_limit: z.number(),
  monthly_tokens_used: z.number(),
  monthly_tokens_remaining: z.number(),
  monthly_cost_limit: decimal,
  monthly_cost_used: decimal,
  monthly_cost_remaining: decimal,
  max_concurrent_operations: z.number(),
  operations_running: z.number(),
});
export type WorkspaceAllowance = z.infer<typeof workspaceAllowanceSchema>;

const userAllowanceSchema = z.object({
  daily_token_limit: z.number(),
  daily_tokens_used: z.number(),
  daily_tokens_remaining: z.number(),
  daily_question_limit: z.number(),
  daily_questions_used: z.number(),
  max_concurrent_generations: z.number(),
  generations_running: z.number(),
  resets_at: z.string(),
});
export type UserAllowance = z.infer<typeof userAllowanceSchema>;

export const usageSummarySchema = z.object({
  workspace_id: z.uuid(),
  scope: z.enum(["workspace", "own"]),
  period_start: z.string(),
  period_end: z.string(),
  currency: z.string(),
  pricing_version: z.string(),
  totals: totalsSchema,
  by_operation: z.array(totalsSchema.extend({ operation: operationSchema })),
  by_model: z.array(totalsSchema.extend({ model: z.string() })),
  daily: z.array(
    z.object({
      date: z.string(),
      chat_tokens: z.number(),
      embedding_tokens: z.number(),
      estimated_cost: decimal,
    }),
  ),
  estimated_records: z.number(),
  unavailable_records: z.number(),
  accounting: z
    .object({
      provider_reported_tokens: z.number(),
      estimated_tokens: z.number(),
      held_tokens: z.number(),
      not_charged_records: z.number(),
    })
    .optional(),
  outcomes: z.record(z.string(), z.number()).optional(),
  allowance: workspaceAllowanceSchema,
  my_allowance: userAllowanceSchema,
});
export type UsageSummary = z.infer<typeof usageSummarySchema>;

export const usageEventSchema = z.object({
  id: z.uuid(),
  operation: operationSchema,
  provider: z.string(),
  model: z.string(),
  status: z.enum(["reserved", "completed", "failed", "cancelled", "expired"]),
  usage_source: z.enum(["provider", "estimated", "unavailable", "none"]).nullable(),
  charged_tokens: z.number(),
  charged_cost: decimal,
  currency: z.string(),
  error_code: z.string().nullable(),
  created_at: z.string(),
  user_id: z.uuid().nullable().optional(),
});
export type UsageEvent = z.infer<typeof usageEventSchema>;

const usageHistorySchema = z.object({
  items: z.array(usageEventSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});
export type UsageHistory = z.infer<typeof usageHistorySchema>;

export const HISTORY_PAGE_SIZE = 20;

export const alertSchema = z.object({
  id: z.uuid(),
  kind: z.enum(["workspace_tokens", "workspace_cost", "user_daily_tokens"]),
  threshold_percent: z.number(),
  period_start: z.string(),
  used: decimal,
  limit_value: decimal,
  created_at: z.string(),
});
export type UsageAlert = z.infer<typeof alertSchema>;

export const ALERT_LABELS: Record<UsageAlert["kind"], string> = {
  workspace_tokens: "this workspace's monthly tokens",
  workspace_cost: "this workspace's monthly spending",
  user_daily_tokens: "your daily tokens",
};

export const getUsageAlerts = (workspaceId: string, signal?: AbortSignal) =>
  apiFetch(`/workspaces/${workspaceId}/usage/alerts`, z.array(alertSchema), { signal });

export const usageKeys = {
  summary: (workspaceId: string) => ["workspaces", workspaceId, "usage"] as const,
  alerts: (workspaceId: string) => ["workspaces", workspaceId, "usage", "alerts"] as const,
  history: (workspaceId: string, offset: number, operation: Operation | "") =>
    ["workspaces", workspaceId, "usage", "history", offset, operation] as const,
};

export const getUsageSummary = (workspaceId: string, signal?: AbortSignal) =>
  apiFetch(`/workspaces/${workspaceId}/usage`, usageSummarySchema, { signal });

export function getUsageHistory(
  workspaceId: string,
  offset: number,
  operation: Operation | "",
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE), offset: String(offset) });
  if (operation) params.set("operation", operation);
  return apiFetch(`/workspaces/${workspaceId}/usage/history?${params}`, usageHistorySchema, { signal });
}

/** The share of a limit used, from 0 to 1 (1 when the limit is 0 or exceeded). */
export function share(used: number, limit: number): number {
  if (limit <= 0) return 1;
  return Math.min(1, Math.max(0, used / limit));
}

/** From this share on, a limit gets a warning. */
export const WARN_AT = 0.8;

export interface QuotaWarning {
  tone: "warning" | "exceeded";
  message: string;
}

/** Warnings for limits that are nearly or fully used, from the API's own numbers. */
export function quotaWarnings(summary: UsageSummary): QuotaWarning[] {
  const warnings: QuotaWarning[] = [];
  const add = (fraction: number, what: string) => {
    if (fraction >= 1) warnings.push({ tone: "exceeded", message: `${what} is used up.` });
    else if (fraction >= WARN_AT)
      warnings.push({ tone: "warning", message: `${Math.round(fraction * 100)}% of ${what} is used.` });
  };
  const { allowance: a, my_allowance: me } = summary;
  add(share(a.monthly_tokens_used, a.monthly_token_limit), "this workspace's monthly token allowance");
  add(share(a.monthly_cost_used, a.monthly_cost_limit), "this workspace's monthly spending limit");
  add(share(me.daily_tokens_used, me.daily_token_limit), "your daily token allowance");
  add(share(me.daily_questions_used, me.daily_question_limit), "your daily question allowance");
  return warnings;
}

const numberFormat = new Intl.NumberFormat();
export const formatTokens = (value: number) => numberFormat.format(value);

export function formatCost(value: number, currency: string): string {
  // Small amounts keep enough digits to be meaningful; a real but tiny cost is never shown as 0.
  const format = (amount: number, digits: number) =>
    new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(amount);
  if (value > 0 && value < 0.0001) return `< ${format(0.0001, 4)}`;
  return format(value, value !== 0 && Math.abs(value) < 0.01 ? 4 : 2);
}

export const usagePath = (workspaceId: string) => `/dashboard/workspaces/${workspaceId}/usage`;
