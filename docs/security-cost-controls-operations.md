# Security and cost controls: operations

How to configure limits and prices, read usage, and recover from problems. Architecture:
[security-cost-controls-architecture.md](security-cost-controls-architecture.md).

## Environment variables

All are read by the API at startup (`app/core/config.py`) and passed through
`docker-compose.yml`. Defaults suit local development. Set real values per deployment in
`.env` (gitignored) or the platform's secret store, never in the repository. Invalid values
stop the API from starting.

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMITS_ENABLED` | `true` | Switches request rate limits off (quotas stay on). |
| `RATE_LIMIT_CHAT_PER_MINUTE` | 10 | Questions per user per minute, `/chat` and `/chat/stream` together. |
| `RATE_LIMIT_CHAT_PER_WORKSPACE_PER_MINUTE` | 60 | Questions per workspace per minute. |
| `RATE_LIMIT_SEARCH_PER_MINUTE` | 30 | Searches per user per minute. |
| `RATE_LIMIT_UPLOAD_PER_HOUR` | 10 | Uploads per user per hour. |
| `MAX_CONCURRENT_GENERATIONS_PER_USER` | 3 | Answers being written at once per user. |
| `MAX_CONCURRENT_AI_OPERATIONS_PER_WORKSPACE` | 10 | AI operations of any kind at once per workspace. |
| `MAX_INGESTION_JOBS_PER_WORKSPACE` | 5 | Documents waiting for or undergoing ingestion per workspace. |
| `USER_DAILY_TOKEN_LIMIT` | 500000 | Tokens per user per UTC day, all workspaces. |
| `USER_DAILY_CHAT_LIMIT` | 200 | Questions per user per UTC day. |
| `WORKSPACE_MONTHLY_TOKEN_LIMIT` | 5000000 | Tokens per workspace per UTC month. |
| `WORKSPACE_MONTHLY_COST_LIMIT` | 10.00 | Estimated spend per workspace per UTC month, in the pricing currency. |
| `CHAT_MAX_INPUT_TOKENS` | 12000 | The most estimated input one answer may send. |
| `CHAT_MAX_OUTPUT_TOKENS` | 1000 | Existing setting: the output cap sent to the model, and the output reserved. |
| `USAGE_RESERVATION_TTL_SECONDS` | 600 | When an unfinished answer's reservation is treated as abandoned. Must exceed `CHAT_GENERATION_LEASE_SECONDS`. |
| `INGESTION_RESERVATION_TTL_SECONDS` | 3600 | The same for a document's embeddings. |
| `USAGE_PRICING_JSON` | empty | Replaces the default price table (below). |

Changing a limit takes effect when the API restarts:

```bash
docker compose up -d api
```

## Changing model prices

The default table is `DEFAULT_PRICING` in `apps/api/app/core/config.py`. To change prices
without a code change, set `USAGE_PRICING_JSON` to a whole table, with a **new version label**:

```json
{"version": "openai-2026-12", "currency": "USD",
 "models": {"gpt-4.1-mini": {"input_per_million": "0.40", "output_per_million": "1.60"},
            "text-embedding-3-small": {"input_per_million": "0.02", "output_per_million": "0"}}}
```

- Prices are per million tokens, as decimal strings. Model names match by prefix, so dated
  snapshots use their family's price.
- New records use the new table. Existing records keep their `pricing_version` and cost:
  history is not repriced.
- A model missing from the table is recorded with no cost. Spending limits cannot see it;
  token limits still apply. Add every model you configure (`OPENAI_CHAT_MODEL`,
  `EMBEDDING_MODEL`).
- Check prices against the provider's pricing page. These are estimates, not an invoice.

## Changing a workspace's limits

Owners can tighten their workspace's limits (never above the system limits):

```bash
curl -X PATCH "$API/api/v1/workspaces/$WORKSPACE/quotas" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"monthly_token_limit": 1000000, "monthly_cost_limit": "5.00", "max_concurrent_operations": 4}'
```

Send `null` for a field to return it to the system limit. Raising a limit beyond the system
value requires changing the environment variable, which is an operator decision.

## Reading usage

- **In the app:** Workspace → **Usage**. Owners and admins see the whole workspace; members
  see their own usage and the workspace's remaining allowance.
- **Over the API:** see the endpoint table in the architecture document.
- **In SQL**, for operators:

```sql
-- This month, by workspace and operation.
SELECT workspace_id, operation, sum(charged_tokens), sum(charged_cost), count(*)
FROM usage_events
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
GROUP BY 1, 2 ORDER BY 4 DESC;

-- How much usage was estimated or unknown rather than reported.
SELECT usage_source, count(*), sum(charged_tokens) FROM usage_events GROUP BY 1;
```

## Operational recovery

| Situation | What happens | What to do |
|---|---|---|
| API crashed mid-answer | Lease frees after 300 s; reservation expires after `USAGE_RESERVATION_TTL_SECONDS`, charged at its reserved amount. | Nothing. To release at once, mark it expired (below). |
| Reservations piling up | Shown as "running" in the dashboard. | Check API logs for crashes. Expire stale ones manually if needed (below). |
| Users hitting `usage_unavailable` (503) | The database could not be reached for the usage check; requests fail closed. | Fix database connectivity. No data is lost. |
| A workspace hit its monthly limit early | 429 `workspace_quota_exceeded` until the 1st (UTC). | Raise the system limit, or remove the workspace's own stricter override. |
| Price change | Costs are estimated with the old table. | Set `USAGE_PRICING_JSON` with a new version. Old rows keep their version. |
| Rate-limit table growing | Old windows are removed per subject as it is used; idle subjects keep at most two rows per policy. | Optional: `DELETE FROM rate_limit_buckets WHERE window_start < now() - interval '1 day';` |

Expire abandoned reservations immediately (they stay charged at their reserved amount):

```sql
UPDATE usage_events
SET status = 'expired', usage_source = 'unavailable', error_code = 'reservation_expired',
    completed_at = now()
WHERE status = 'reserved' AND expires_at < now();
```

## Known limitations

- **Estimates are not invoices.** The tokenizer differs slightly from the model's, prices are
  list prices, and cached-input discounts are ignored.
- **Cancelled streams are estimated.** The provider may have generated a few more tokens
  after the disconnect than were received.
- **Failed non-streamed calls charge the reservation.** When nothing is known, a timeout is
  charged its maximum. That is conservative and can overstate.
- **Query embeddings are not reserved.** Two searches at the same moment can exceed a quota by
  a few tokens each.
- **Expired reservations keep their reserved amount charged.** A crash before the provider was
  called is overstated until the period resets.
- **Uploads within one workspace are serialised** while the ingestion cap is checked and the
  file is stored. This is bounded by the upload rate limit.
- **Daily question limits count attempts,** including failed ones.
- **No per-user overrides.** User limits are system-wide; workspace overrides only tighten.
- **No alerting or email notifications** on approaching limits; the dashboard shows warnings
  from 80%.
