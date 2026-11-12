/** A usage summary as the API sends it, for tests. */
export const USAGE_WORKSPACE = "8a5c1f0e-0000-4000-8000-000000000000";
const WORKSPACE = USAGE_WORKSPACE;

export function summaryPayload(overrides: Record<string, unknown> = {}) {
  return {
    workspace_id: WORKSPACE,
    scope: "workspace",
    period_start: "2026-11-01T00:00:00Z",
    period_end: "2026-12-01T00:00:00Z",
    currency: "USD",
    pricing_version: "openai-list-2025-04",
    totals: { tokens: 1500, estimated_cost: "0.00180000", operations: 4 },
    by_operation: [
      { operation: "chat_stream", tokens: 1200, estimated_cost: "0.00170000", operations: 2 },
      { operation: "document_embedding", tokens: 300, estimated_cost: "0.00001000", operations: 2 },
    ],
    by_model: [
      { model: "gpt-4.1-mini-2025-04-14", tokens: 1200, estimated_cost: "0.00170000", operations: 2 },
    ],
    daily: [{ date: "2026-11-12", chat_tokens: 1200, embedding_tokens: 300, estimated_cost: "0.00180000" }],
    estimated_records: 1,
    unavailable_records: 0,
    allowance: {
      monthly_token_limit: 10000,
      monthly_tokens_used: 1500,
      monthly_tokens_remaining: 8500,
      monthly_cost_limit: "10.00",
      monthly_cost_used: "0.0018",
      monthly_cost_remaining: "9.9982",
      max_concurrent_operations: 10,
      operations_running: 0,
    },
    my_allowance: {
      daily_token_limit: 500000,
      daily_tokens_used: 1500,
      daily_tokens_remaining: 498500,
      daily_question_limit: 200,
      daily_questions_used: 2,
      max_concurrent_generations: 3,
      generations_running: 0,
      resets_at: "2026-11-13T00:00:00Z",
    },
    ...overrides,
  };
}
