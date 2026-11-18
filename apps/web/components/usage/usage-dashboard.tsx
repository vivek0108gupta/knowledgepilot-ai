"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useId, useState } from "react";

import { PageHeader } from "@/components/dashboard/shell";
import { Button, buttonClass } from "@/components/ui/button";
import { Alert, EmptyState, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { ApiError, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  ALERT_LABELS,
  HISTORY_PAGE_SIZE,
  OPERATION_LABELS,
  formatCost,
  formatTokens,
  getUsageAlerts,
  getUsageHistory,
  getUsageSummary,
  operationSchema,
  quotaWarnings,
  share,
  usageKeys,
  type Operation,
  type UsageSummary,
} from "@/lib/usage";
import { getWorkspace, workspaceKeys } from "@/lib/workspaces";

const dayFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", timeZone: "UTC" });
const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function Panel({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5">
      <h2 className="text-base font-semibold text-zinc-900">{title}</h2>
      {description && <p className="mt-1 text-sm text-zinc-600">{description}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

/** A labelled meter. The numbers are always written out, so nothing depends on the bar. */
function Meter({
  label,
  used,
  limit,
  detail,
}: {
  label: string;
  used: number;
  limit: number;
  detail: string;
}) {
  const fraction = share(used, limit);
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4">
      <p className="text-sm text-zinc-600">{label}</p>
      <p className="mt-1 text-xl font-semibold text-zinc-900">{detail}</p>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={Math.min(used, limit)}
        aria-valuetext={detail}
        className="mt-3 h-2 overflow-hidden rounded-full bg-zinc-100"
      >
        <div
          className={cn(
            "h-full rounded-full",
            fraction >= 1 ? "bg-red-600" : fraction >= 0.8 ? "bg-amber-500" : "bg-zinc-900",
          )}
          style={{ width: `${Math.round(fraction * 100)}%` }}
        />
      </div>
    </div>
  );
}

function DailyChart({ summary }: { summary: UsageSummary }) {
  const titleId = useId();
  const days = summary.daily;
  if (days.length === 0) return <p className="text-sm text-zinc-600">No usage yet this month.</p>;
  const peak = Math.max(...days.map((day) => day.chat_tokens + day.embedding_tokens), 1);
  return (
    <figure aria-labelledby={titleId}>
      <figcaption id={titleId} className="sr-only">
        Tokens used per day this month, split into answers and embeddings. The same numbers are in the table
        below.
      </figcaption>
      <div
        aria-hidden="true"
        className="flex h-40 items-end gap-1 overflow-x-auto border-b border-zinc-200 pb-px"
      >
        {days.map((day) => {
          const total = day.chat_tokens + day.embedding_tokens;
          return (
            <div
              key={day.date}
              className="flex min-w-3 flex-1 flex-col justify-end"
              style={{ height: `${(total / peak) * 100}%` }}
              title={`${dayFormat.format(new Date(day.date))}: ${formatTokens(total)} tokens`}
            >
              <div className="bg-sky-500" style={{ flexGrow: day.embedding_tokens }} />
              <div className="rounded-t-sm bg-zinc-900" style={{ flexGrow: day.chat_tokens }} />
            </div>
          );
        })}
      </div>
      <div aria-hidden="true" className="mt-2 flex flex-wrap gap-4 text-xs text-zinc-600">
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm bg-zinc-900" /> Answers
        </span>
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm bg-sky-500" /> Embeddings
        </span>
      </div>
      <details className="mt-3 text-sm">
        <summary className="cursor-pointer text-zinc-700">Show as a table</summary>
        <table className="mt-2 w-full text-left text-sm">
          <thead className="text-zinc-500">
            <tr>
              <th className="py-1 font-medium">Day (UTC)</th>
              <th className="py-1 text-right font-medium">Answers</th>
              <th className="py-1 text-right font-medium">Embeddings</th>
              <th className="py-1 text-right font-medium">Estimated cost</th>
            </tr>
          </thead>
          <tbody>
            {days.map((day) => (
              <tr key={day.date} className="border-t border-zinc-100">
                <td className="py-1">{dayFormat.format(new Date(day.date))}</td>
                <td className="py-1 text-right tabular-nums">{formatTokens(day.chat_tokens)}</td>
                <td className="py-1 text-right tabular-nums">{formatTokens(day.embedding_tokens)}</td>
                <td className="py-1 text-right tabular-nums">
                  {formatCost(day.estimated_cost, summary.currency)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

function BreakdownTable({
  caption,
  rows,
  currency,
}: {
  caption: string;
  rows: Array<{ key: string; label: string; tokens: number; estimated_cost: number; operations: number }>;
  currency: string;
}) {
  if (rows.length === 0) return <p className="text-sm text-zinc-600">Nothing recorded yet this month.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[28rem] text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="text-zinc-500">
          <tr>
            <th scope="col" className="py-1.5 font-medium">
              {caption.startsWith("By model") ? "Model" : "Operation"}
            </th>
            <th scope="col" className="py-1.5 text-right font-medium">
              Requests
            </th>
            <th scope="col" className="py-1.5 text-right font-medium">
              Tokens
            </th>
            <th scope="col" className="py-1.5 text-right font-medium">
              Estimated cost
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-zinc-100">
              <th scope="row" className="py-1.5 font-normal break-all text-zinc-900">
                {row.label}
              </th>
              <td className="py-1.5 text-right tabular-nums">{formatTokens(row.operations)}</td>
              <td className="py-1.5 text-right tabular-nums">{formatTokens(row.tokens)}</td>
              <td className="py-1.5 text-right tabular-nums">{formatCost(row.estimated_cost, currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const STATUS_LABELS = {
  reserved: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
  expired: "Abandoned",
} as const;

const SOURCE_LABELS = {
  provider: "Reported by the provider",
  estimated: "Estimated",
  unavailable: "Held (outcome unknown)",
  none: "Not billed",
} as const;

function History({ workspaceId, showUser }: { workspaceId: string; showUser: boolean }) {
  const [offset, setOffset] = useState(0);
  const [operation, setOperation] = useState<Operation | "">("");
  const filterId = useId();
  const history = useQuery({
    queryKey: usageKeys.history(workspaceId, offset, operation),
    queryFn: ({ signal }) => getUsageHistory(workspaceId, offset, operation, signal),
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={filterId} className="text-sm text-zinc-700">
          Show
        </label>
        <select
          id={filterId}
          value={operation}
          onChange={(event) => {
            const parsed = operationSchema.safeParse(event.target.value);
            setOperation(parsed.success ? parsed.data : "");
            setOffset(0);
          }}
          className="rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-zinc-900"
        >
          <option value="">All operations</option>
          {operationSchema.options.map((value) => (
            <option key={value} value={value}>
              {OPERATION_LABELS[value]}
            </option>
          ))}
        </select>
      </div>

      {history.isPending ? (
        <LoadingRegion label="Loading usage history">
          <Skeleton className="h-32" />
        </LoadingRegion>
      ) : history.isError ? (
        <ErrorState
          title="Could not load usage history"
          description={errorMessage(history.error)}
          onRetry={() => void history.refetch()}
        />
      ) : history.data.items.length === 0 ? (
        <EmptyState title="No usage recorded" description="AI operations appear here once they run." />
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-left text-sm">
              <caption className="sr-only">Usage history, newest first</caption>
              <thead className="text-zinc-500">
                <tr>
                  <th scope="col" className="py-1.5 font-medium">
                    When
                  </th>
                  <th scope="col" className="py-1.5 font-medium">
                    Operation
                  </th>
                  <th scope="col" className="py-1.5 font-medium">
                    Status
                  </th>
                  <th scope="col" className="py-1.5 text-right font-medium">
                    Tokens
                  </th>
                  <th scope="col" className="py-1.5 text-right font-medium">
                    Estimated cost
                  </th>
                  <th scope="col" className="py-1.5 font-medium">
                    Counted from
                  </th>
                  {showUser && (
                    <th scope="col" className="py-1.5 font-medium">
                      User
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {history.data.items.map((item) => (
                  <tr key={item.id} className="border-t border-zinc-100">
                    <td className="py-1.5 whitespace-nowrap">
                      {timeFormat.format(new Date(item.created_at))}
                    </td>
                    <td className="py-1.5">
                      {OPERATION_LABELS[item.operation]}
                      <span className="block text-xs text-zinc-500">{item.model}</span>
                    </td>
                    <td className="py-1.5">{STATUS_LABELS[item.status]}</td>
                    <td className="py-1.5 text-right tabular-nums">{formatTokens(item.charged_tokens)}</td>
                    <td className="py-1.5 text-right tabular-nums">
                      {formatCost(item.charged_cost, item.currency)}
                    </td>
                    <td className="py-1.5 text-xs text-zinc-600">
                      {item.usage_source ? SOURCE_LABELS[item.usage_source] : "Pending"}
                    </td>
                    {showUser && (
                      <td className="py-1.5 font-mono text-xs text-zinc-600">{item.user_id?.slice(0, 8)}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between gap-2 text-sm text-zinc-600">
            <span>
              {offset + 1}–{offset + history.data.items.length} of {formatTokens(history.data.total)}
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - HISTORY_PAGE_SIZE))}
              >
                Newer
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={offset + HISTORY_PAGE_SIZE >= history.data.total}
                onClick={() => setOffset(offset + HISTORY_PAGE_SIZE)}
              >
                Older
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

const alertTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

/** Thresholds the API recorded as crossed this period. Shown only when there are some. */
function RecordedAlerts({ workspaceId }: { workspaceId: string }) {
  const alerts = useQuery({
    queryKey: usageKeys.alerts(workspaceId),
    queryFn: ({ signal }) => getUsageAlerts(workspaceId, signal),
  });
  if (!alerts.data || alerts.data.length === 0) return null;
  return (
    <section aria-label="Usage alerts" className="rounded-xl border border-amber-200 bg-amber-50 p-4">
      <h2 className="text-sm font-semibold text-amber-900">Usage alerts</h2>
      <ul className="mt-2 flex flex-col gap-1 text-sm text-amber-900">
        {alerts.data.map((alert) => (
          <li key={alert.id}>
            {alert.threshold_percent}% of {ALERT_LABELS[alert.kind]} reached ·{" "}
            <time dateTime={alert.created_at}>{alertTime.format(new Date(alert.created_at))}</time>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Which prices the estimates use, and how current they are. */
function pricingNote(summary: UsageSummary): string {
  const dates = [
    summary.pricing_effective_from && `prices effective ${summary.pricing_effective_from}`,
    summary.pricing_verified_on && `last checked ${summary.pricing_verified_on}`,
  ].filter(Boolean);
  const when = dates.length ? `, ${dates.join(", ")}` : "";
  return `Costs are estimates from price table ${summary.pricing_version} (${summary.currency}${when}), not an invoice.`;
}

function Overview({ summary }: { summary: UsageSummary }) {
  const { allowance: a, my_allowance: me, currency } = summary;
  const warnings = quotaWarnings(summary);
  const chat = summary.by_operation
    .filter((row) => row.operation === "chat" || row.operation === "chat_stream")
    .reduce((sum, row) => sum + row.tokens, 0);
  const embedding = summary.totals.tokens - chat;

  return (
    <div className="flex flex-col gap-6">
      {warnings.map((warning) => (
        <Alert key={warning.message} tone={warning.tone === "exceeded" ? "error" : "info"}>
          {warning.message}{" "}
          {warning.tone === "exceeded"
            ? "New AI requests are refused until it resets."
            : "Requests are refused once it is used up."}
        </Alert>
      ))}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Meter
          label="Workspace tokens this month"
          used={a.monthly_tokens_used}
          limit={a.monthly_token_limit}
          detail={`${formatTokens(a.monthly_tokens_used)} of ${formatTokens(a.monthly_token_limit)}`}
        />
        <Meter
          label="Workspace estimated cost this month"
          used={a.monthly_cost_used}
          limit={a.monthly_cost_limit}
          detail={`${formatCost(a.monthly_cost_used, currency)} of ${formatCost(a.monthly_cost_limit, currency)}`}
        />
        <Meter
          label="Your tokens today"
          used={me.daily_tokens_used}
          limit={me.daily_token_limit}
          detail={`${formatTokens(me.daily_tokens_used)} of ${formatTokens(me.daily_token_limit)}`}
        />
        <Meter
          label="Your questions today"
          used={me.daily_questions_used}
          limit={me.daily_question_limit}
          detail={`${formatTokens(me.daily_questions_used)} of ${formatTokens(me.daily_question_limit)}`}
        />
      </div>

      <Panel
        title={
          summary.scope === "workspace"
            ? "This month in this workspace"
            : "Your usage in this workspace this month"
        }
        description={`${formatTokens(summary.totals.tokens)} tokens in ${formatTokens(summary.totals.operations)} operations, about ${formatCost(summary.totals.estimated_cost, currency)}. Answers: ${formatTokens(chat)} tokens. Embeddings: ${formatTokens(embedding)} tokens.`}
      >
        <DailyChart summary={summary} />
      </Panel>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Answers and embeddings">
          <BreakdownTable
            caption="By operation"
            currency={currency}
            rows={summary.by_operation.map((row) => ({
              key: row.operation,
              label: OPERATION_LABELS[row.operation],
              ...row,
            }))}
          />
        </Panel>
        <Panel title="Models">
          <BreakdownTable
            caption="By model"
            currency={currency}
            rows={summary.by_model.map((row) => ({ key: row.model, label: row.model, ...row }))}
          />
        </Panel>
      </div>

      {summary.accounting && (
        <Panel
          title="How these numbers were counted"
          description="Tokens reported by the AI provider are exact. When it reported none, they are counted here (estimated), or, when it is unknown whether the work ran (for example a timeout), the most it could have used is held against your limits."
        >
          <dl className="grid gap-3 sm:grid-cols-3">
            {[
              ["Reported by the provider", summary.accounting.provider_reported_tokens],
              ["Estimated", summary.accounting.estimated_tokens],
              ["Held (outcome unknown)", summary.accounting.held_tokens],
            ].map(([label, value]) => (
              <div key={label as string} className="rounded-lg border border-zinc-200 p-3">
                <dt className="text-xs text-zinc-500">{label}</dt>
                <dd className="mt-0.5 text-lg font-semibold text-zinc-900 tabular-nums">
                  {formatTokens(value as number)}
                </dd>
              </div>
            ))}
          </dl>
        </Panel>
      )}

      <p className="text-xs text-zinc-500">
        {pricingNote(summary)} Totals include usage held while an outcome is unknown (see above).
        {summary.estimated_records + summary.unavailable_records > 0 &&
          ` ${formatTokens(summary.estimated_records + summary.unavailable_records)} records were estimated or charged at their reserved amount because the provider reported no usage (for example a stopped answer).`}{" "}
        Limits reset at midnight UTC (yours) and on the first of the month (the workspace&apos;s).{" "}
        {a.operations_running > 0 && `${a.operations_running} AI operation(s) running now.`}
      </p>
    </div>
  );
}

/** Usage and remaining allowance for one workspace. Every number comes from the API. */
export function UsageDashboard() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const workspace = useQuery({
    queryKey: workspaceKeys.detail(workspaceId),
    queryFn: ({ signal }) => getWorkspace(workspaceId, signal),
  });
  const summary = useQuery({
    queryKey: usageKeys.summary(workspaceId),
    queryFn: ({ signal }) => getUsageSummary(workspaceId, signal),
    refetchInterval: 60_000,
  });

  const back = (
    <Link href={`/dashboard/workspaces/${workspaceId}`} className={buttonClass("secondary", "sm")}>
      Back to workspace
    </Link>
  );

  if (summary.isPending) {
    return (
      <LoadingRegion label="Loading usage">
        <div className="flex flex-col gap-4">
          <Skeleton className="h-9 w-64" />
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {[0, 1, 2, 3].map((index) => (
              <Skeleton key={index} className="h-24" />
            ))}
          </div>
          <Skeleton className="h-56" />
        </div>
      </LoadingRegion>
    );
  }

  if (summary.isError) {
    const missing = summary.error instanceof ApiError && [404, 422].includes(summary.error.status);
    return (
      <div className="flex flex-col items-start gap-4">
        <div className="w-full">
          <ErrorState
            title={missing ? "Workspace not found" : "Could not load usage"}
            description={
              missing
                ? "It may have been deleted, or you may not be a member of it."
                : errorMessage(summary.error)
            }
            onRetry={missing ? undefined : () => void summary.refetch()}
          />
        </div>
        {back}
      </div>
    );
  }

  const data = summary.data;
  return (
    <>
      <PageHeader
        title="Usage"
        description={`${workspace.data?.name ?? "Workspace"} · ${
          data.scope === "workspace"
            ? "Everyone's usage in this workspace"
            : "Your own usage in this workspace"
        }`}
        action={back}
      />
      <div className="flex flex-col gap-6">
        <RecordedAlerts workspaceId={workspaceId} />
        <Overview summary={data} />
        <Panel
          title="History"
          description={
            data.scope === "workspace"
              ? "Every AI operation in this workspace."
              : "Your AI operations in this workspace."
          }
        >
          <History workspaceId={workspaceId} showUser={data.scope === "workspace"} />
        </Panel>
      </div>
    </>
  );
}
