import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { usageSummarySchema } from "@/lib/usage";
import { summaryPayload } from "@/test/usage-fixtures";
import { renderWithQuery } from "@/test/render";

import { UsageDashboard } from "./usage-dashboard";

const WORKSPACE = "8a5c1f0e-0000-4000-8000-000000000000";

const mocks = vi.hoisted(() => ({
  getUsageSummary: vi.fn(),
  getUsageHistory: vi.fn(),
  getUsageAlerts: vi.fn(),
  getWorkspace: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useParams: () => ({ workspaceId: WORKSPACE }) }));
vi.mock("@/lib/usage", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/usage")>()),
  getUsageSummary: mocks.getUsageSummary,
  getUsageHistory: mocks.getUsageHistory,
  getUsageAlerts: mocks.getUsageAlerts,
}));
vi.mock("@/lib/workspaces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workspaces")>()),
  getWorkspace: mocks.getWorkspace,
}));

const summary = (overrides: Record<string, unknown> = {}) =>
  usageSummarySchema.parse(summaryPayload(overrides));

function event(index: number, overrides: Record<string, unknown> = {}) {
  return {
    id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    operation: "chat_stream",
    provider: "openai",
    model: "gpt-4.1-mini-2025-04-14",
    status: "completed",
    usage_source: "provider",
    charged_tokens: 600,
    charged_cost: 0.00085,
    currency: "USD",
    error_code: null,
    created_at: "2026-11-12T10:00:00Z",
    user_id: "33333333-3333-4333-8333-333333333333",
    ...overrides,
  };
}

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.getWorkspace.mockResolvedValue({
    id: WORKSPACE,
    name: "Research",
    owner_id: "33333333-3333-4333-8333-333333333333",
    role: "owner",
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
  });
  mocks.getUsageSummary.mockResolvedValue(summary());
  mocks.getUsageAlerts.mockResolvedValue([]);
  mocks.getUsageHistory.mockResolvedValue({
    items: [event(1), event(2, { status: "cancelled", usage_source: "estimated" })],
    total: 2,
    limit: 20,
    offset: 0,
  });
});

describe("usage dashboard", () => {
  test("shows a loading state, then the API's numbers", async () => {
    renderWithQuery(<UsageDashboard />);

    expect(screen.getByRole("status", { name: "Loading usage" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Usage" })).toBeInTheDocument();

    const tokens = screen.getByRole("meter", { name: "Workspace tokens this month" });
    expect(tokens).toHaveAttribute("aria-valuetext", "1,500 of 10,000");
    expect(screen.getByRole("meter", { name: "Your questions today" })).toHaveAttribute(
      "aria-valuetext",
      "2 of 200",
    );
    expect(screen.getByText(/Everyone's usage in this workspace/)).toBeInTheDocument();
    expect(screen.getByText(/not an invoice/)).toHaveTextContent("openai-list-2025-04");
    expect(screen.getByText(/not an invoice/)).toHaveTextContent("Totals include usage held");
    expect(screen.getByText(/1 records were estimated/)).toBeInTheDocument();
  });

  test("breaks usage down by operation, model and day", async () => {
    renderWithQuery(<UsageDashboard />);

    const operations = await screen.findByRole("table", { name: "By operation" });
    expect(within(operations).getByRole("rowheader", { name: "Streamed answers" })).toBeInTheDocument();
    expect(within(operations).getByRole("rowheader", { name: "Document indexing" })).toBeInTheDocument();
    expect(
      within(screen.getByRole("table", { name: "By model" })).getByText("gpt-4.1-mini-2025-04-14"),
    ).toBeInTheDocument();
    expect(screen.getByRole("figure", { name: /Tokens used per day/ })).toBeInTheDocument();
    expect(screen.getByText(/Answers: 1,200 tokens. Embeddings: 300 tokens/)).toBeInTheDocument();
  });

  test("history is a table with paging and a filter", async () => {
    const user = userEvent.setup();
    renderWithQuery(<UsageDashboard />);

    const table = await screen.findByRole("table", { name: "Usage history, newest first" });
    expect(within(table).getByText("Stopped")).toBeInTheDocument();
    expect(within(table).getByText("Estimated")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "User" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Newer" })).toBeDisabled();

    await user.selectOptions(screen.getByRole("combobox", { name: "Show" }), "document_embedding");

    expect(mocks.getUsageHistory).toHaveBeenLastCalledWith(
      WORKSPACE,
      0,
      "document_embedding",
      expect.anything(),
    );
  });

  test("members see their own usage, without a user column", async () => {
    mocks.getUsageSummary.mockResolvedValue(summary({ scope: "own" }));
    renderWithQuery(<UsageDashboard />);

    expect(await screen.findByText(/Your own usage in this workspace/)).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Usage history, newest first" });
    expect(within(table).queryByRole("columnheader", { name: "User" })).not.toBeInTheDocument();
  });

  test("warns when a limit is nearly or fully used", async () => {
    mocks.getUsageSummary.mockResolvedValue(
      summary({
        allowance: { ...summaryPayload().allowance, monthly_tokens_used: 10000, monthly_tokens_remaining: 0 },
      }),
    );
    renderWithQuery(<UsageDashboard />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("this workspace's monthly token allowance is used up.");
    expect(alert).toHaveTextContent("New AI requests are refused until it resets.");
  });

  test("an empty month and an empty history say so", async () => {
    mocks.getUsageSummary.mockResolvedValue(
      summary({
        daily: [],
        by_operation: [],
        by_model: [],
        totals: { tokens: 0, estimated_cost: "0", operations: 0 },
      }),
    );
    mocks.getUsageHistory.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
    renderWithQuery(<UsageDashboard />);

    expect(await screen.findByText("No usage yet this month.")).toBeInTheDocument();
    expect(await screen.findByText("No usage recorded")).toBeInTheDocument();
  });

  test("errors offer a retry, and a missing workspace says so", async () => {
    mocks.getUsageSummary.mockRejectedValueOnce(
      new ApiError(503, "usage_unavailable", "Usage limits could not be checked."),
    );
    const user = userEvent.setup();
    renderWithQuery(<UsageDashboard />);

    expect(await screen.findByText("Could not load usage")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByRole("heading", { name: "Usage" })).toBeInTheDocument();
  });

  test("a workspace the user is not in shows not found", async () => {
    mocks.getUsageSummary.mockRejectedValue(new ApiError(404, "workspace_not_found", "Workspace not found."));
    renderWithQuery(<UsageDashboard />);

    expect(await screen.findByText("Workspace not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to workspace" })).toBeInTheDocument();
  });

  test("shows how the tokens were counted: reported, estimated or held", async () => {
    mocks.getUsageSummary.mockResolvedValue(
      summary({
        accounting: {
          provider_reported_tokens: 1200,
          estimated_tokens: 100,
          held_tokens: 200,
          not_charged_records: 1,
        },
        outcomes: { executed_with_usage: 3, unknown: 1 },
      }),
    );
    renderWithQuery(<UsageDashboard />);

    const panel = await screen.findByRole("heading", { name: "How these numbers were counted" });
    const section = panel.closest("section")!;
    expect(within(section).getByText("Reported by the provider").nextElementSibling).toHaveTextContent(
      "1,200",
    );
    expect(within(section).getByText("Held (outcome unknown)").nextElementSibling).toHaveTextContent("200");
  });

  test("lists the alerts the API recorded", async () => {
    mocks.getUsageAlerts.mockResolvedValue([
      {
        id: "77777777-7777-4777-8777-777777777777",
        kind: "workspace_cost",
        threshold_percent: 80,
        period_start: "2026-11-01T00:00:00Z",
        used: "8.10",
        limit_value: "10.00",
        created_at: "2026-11-12T10:00:00Z",
      },
    ]);
    renderWithQuery(<UsageDashboard />);

    const alerts = await screen.findByRole("region", { name: "Usage alerts" });
    expect(alerts).toHaveTextContent("80% of this workspace's monthly spending reached");
  });

  test("without alerts there is no alert panel", async () => {
    renderWithQuery(<UsageDashboard />);

    await screen.findByRole("heading", { name: "Usage" });
    expect(screen.queryByRole("region", { name: "Usage alerts" })).not.toBeInTheDocument();
  });

  test("says when the prices took effect and were last checked", async () => {
    mocks.getUsageSummary.mockResolvedValue(
      summary({ pricing_effective_from: "2025-04-14", pricing_verified_on: "2026-11-12" }),
    );
    renderWithQuery(<UsageDashboard />);

    expect(await screen.findByText(/not an invoice/)).toHaveTextContent(
      "Costs are estimates from price table openai-list-2025-04 (USD, prices effective 2025-04-14, last checked 2026-11-12), not an invoice.",
    );
  });

  test("labels held usage in the history as held, not as a reserved amount", async () => {
    mocks.getUsageHistory.mockResolvedValue({
      items: [event(3, { status: "failed", usage_source: "unavailable" })],
      total: 1,
      limit: 20,
      offset: 0,
    });
    renderWithQuery(<UsageDashboard />);

    const table = await screen.findByRole("table", { name: "Usage history, newest first" });
    expect(within(table).getByText("Held (outcome unknown)")).toBeInTheDocument();
    expect(within(table).queryByText(/reserved amount/)).not.toBeInTheDocument();
  });
});
