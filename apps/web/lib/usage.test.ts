import { afterEach, describe, expect, test, vi } from "vitest";

import {
  formatCost,
  getUsageHistory,
  getUsageSummary,
  quotaWarnings,
  share,
  usageSummarySchema,
  type UsageSummary,
} from "./usage";

import { summaryPayload, USAGE_WORKSPACE as WORKSPACE } from "@/test/usage-fixtures";

afterEach(() => vi.unstubAllGlobals());

describe("usage client", () => {
  test("reads the summary, turning decimal strings into numbers", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      calls.push(url);
      return Response.json(summaryPayload());
    });

    const summary = await getUsageSummary(WORKSPACE);

    expect(calls[0]).toBe(`http://localhost:8000/api/v1/workspaces/${WORKSPACE}/usage`);
    expect(summary.totals.estimated_cost).toBeCloseTo(0.0018);
    expect(summary.allowance.monthly_cost_limit).toBe(10);
  });

  test("history is paged and filtered through query parameters", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      calls.push(url);
      return Response.json({ items: [], total: 0, limit: 20, offset: 40 });
    });

    await getUsageHistory(WORKSPACE, 40, "chat");

    expect(calls[0]).toContain("/usage/history?limit=20&offset=40&operation=chat");
  });
});

describe("quota warnings", () => {
  const parse = (overrides: Record<string, unknown>) =>
    usageSummarySchema.parse(summaryPayload(overrides)) as UsageSummary;

  test("nothing to warn about well within the limits", () => {
    expect(quotaWarnings(parse({}))).toEqual([]);
  });

  test("warn from 80% and say when a limit is used up", () => {
    const summary = parse({
      allowance: { ...summaryPayload().allowance, monthly_tokens_used: 8500 },
      my_allowance: { ...summaryPayload().my_allowance, daily_questions_used: 200 },
    });

    expect(quotaWarnings(summary)).toEqual([
      { tone: "warning", message: "85% of this workspace's monthly token allowance is used." },
      { tone: "exceeded", message: "your daily question allowance is used up." },
    ]);
  });

  test("share stays between 0 and 1, and a zero limit counts as used up", () => {
    expect([share(5, 10), share(20, 10), share(-1, 10), share(0, 0)]).toEqual([0.5, 1, 0, 1]);
  });

  test("small costs keep meaningful digits", () => {
    expect(formatCost(0.0018, "USD")).toMatch(/0\.0018/);
    expect(formatCost(12.5, "USD")).toMatch(/12\.50/);
    expect(formatCost(0.00000014, "USD")).toMatch(/^< .*0\.0001$/);
    expect(formatCost(0, "USD")).toMatch(/0\.00/);
  });
});
