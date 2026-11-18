# Phase 5.7: load testing

## Method

`scripts/loadtest/run.py` (with `scripts/loadtest/mocks.py`) runs the following set-up.

**Two real API processes** (uvicorn, `ENVIRONMENT=production`, JSON logs, metrics on):

- each runs its own ingestion workers (`PROCESSING_CONCURRENCY=2`);
- both share one **throwaway PostgreSQL 17 database**, `knowledgepilot_load`, which is
  created and migrated for the run and dropped at the end.

**Local stand-ins** (no outside calls, so there is **no uncontrolled provider load**):

- **OpenAI:** embeddings plus the Responses API, whole and streamed. It models a fast
  provider: the first token after 0.3 s, then about 30 pieces 20 ms apart, and 50 ms per
  embedding call.
- **Supabase Storage:** the REST calls the API makes.
- **Locally signed tokens** (ES256, served from a local JWKS).

**Production limits stay on.** These are the default per-user and per-workspace rate limits,
quotas and concurrency limits. Load comes from many users across 12 workspaces (3 users each),
the way real traffic would.

**Sampled every 0.5 s:**

- CPU and memory of each API process (`ps`);
- the database's connections (`pg_stat_activity`);
- `/metrics`, scraped from a live process at the end.

**Machine:** one laptop (Apple silicon, on battery) running the database in Docker, both API
processes, the mocks and the load generator. So **absolute numbers are a lower bound** for
dedicated servers, and the API processes competed with the load generator for CPU.

**Data:** temporary only. No real provider, no real bucket and no existing data was touched.

### Validity note

Four complete runs were made. **The laptop went to idle sleep during two of them** (macOS
power log: 926 s and repeated 15-minute sleeps), which froze every process. Their results
are discarded:

- they show multi-minute "latencies";
- they show errors caused by connections dropped across the sleep.

The results below come from the run whose scenarios ran with **no gap in activity**. In that
run a 283 s sleep happened while API A was starting, before any scenario; the logs show none
afterwards. A later partial run, also without a gap, reproduced the same figures within
about 10%. Run the test with `caffeinate -i` on a machine on mains power, or on a server.

## Results (2026-11-12, about 2.5 minutes of load)

| Scenario | Load | RPS | p50 | p95 | p99 | Errors | Peak CPU per process | Peak RSS per process | DB connections (peak, active) |
|---|---|---|---|---|---|---|---|---|---|
| Readiness (`/health/ready`, a database round trip) | 3,000 requests, 60 concurrent | 380 | 96 ms | 507 ms | 836 ms | 0% | 21% | 159 MB | 11, 1 |
| Uploads (txt, about 4 KB) | 36 users at once | 127 | 266 ms | 277 ms | 277 ms | 0% | | | |
| Ingestion of those 36 | Both processes' workers | 19 documents/s | all indexed in **1.9 s** | | | 0 failed | 55% | 200 MB | 12, 1 |
| **Semantic search** | 36 users × 20 searches, 72 concurrent | **127** | **468 ms** | **1.12 s** | **1.69 s** | **0%** | **89%** | 179 MB | **31, 21** |
| **Streamed answers** | 36 at once, twice (72) | | first token **1.29 s**, total 1.89 s | first token **1.38 s**, total 2.11 s | first token 1.47 s, total 2.14 s | **0%** | 64% | 191 MB | 31, 10 |
| Rate limit under load | 1 user, 45 searches at once, across both processes | | | | | 30 × 200, **15 × 429**: exactly the limit | | | 25, 13 |
| Quota under load | 9 answers at once against a workspace limit of used + 2.5 reservations | | | | | 2 admitted, **7 refused (429)**; charged 10,630 of 32,962: **never over the limit** | | | |
| **Worker crash under load** (`kill -9` on one process; 24 uploads, slow embeddings) | | | | | | **24/24 indexed by the survivor in 61 s, no duplicate chunks, no stuck jobs**. The 2 jobs that were running on the killed process were recovered after the 30 s lease; their 2 reservations were expired by the sweep. | | | |
| Survivor during recovery | 12 streams | | | | | 11 completed; 1 refused `workspace_quota_exceeded` because it was sent to the workspace the quota scenario had just limited (a harness error, since fixed) | | | |

**From `/metrics` (process A):**

- **Mean provider first token: 0.31 s.** Measured from the model call, this is the mock's
  0.3 s.
- **Mean generation: 1.18 s.**
- **Mean ingestion queue wait: 1.7 s.** This includes the crash scenario, where jobs waited
  out the killed process's lease.
- **`usage_reservations_expired_total` 2:** the killed process's 2 reservations.

**Provider usage (counted by the stand-in):**

- 85 Responses API calls, all streamed: 72 in the stream scenario, 2 admitted in the quota
  scenario and 11 during crash recovery;
- 897 embedding calls for 1,162 texts.

There were no calls for refused requests: refusals happen before any provider call.

**Logs:** 6,595 JSON lines, scanned for `Bearer`, both keys, the metrics token and document
text: **none found**.

## Findings

1. **Correctness held under concurrency.** Across both processes:
   - every upload was indexed exactly once;
   - the per-user rate limit was exact;
   - concurrent answers could not overspend a workspace quota (reservations are taken under
     locks before the call);
   - a crashed worker's jobs and reservations were recovered without duplicates.
2. **Search is CPU-bound** at about 127 requests/s for two processes on this laptop (89% CPU
   each, p95 1.1 s at 72 concurrent). Each search embeds the query, runs a pgvector query and
   checks rate limits in the database. **Scale search with replicas (CPU).** Of the 21 active
   database connections at peak, most were short.
3. **Time to first token is about 1 s above the provider's** at 36 concurrent streams (1.29 s
   at the client against 0.31 s at the provider). The overhead covers:
   - authentication;
   - the rate-limit and quota checks;
   - the usage reservation;
   - history;
   - query embedding and retrieval;
   - the context build.

   On real OpenAI the provider's own first token is about 1.0 to 1.4 s
   ([model evaluation](phase-5.7-model-evaluation.md)), so **expect about 2.3 to 2.7 s to the
   first word at this concurrency** on comparable hardware.
4. **Database connections:**
   - Each API process can hold up to 15 (SQLAlchemy's default pool of 5 plus 10 overflow).
   - At peak the two processes plus the sampler used 31.
   - PostgreSQL's default `max_connections` is 100, so **more than about 6 replicas need a
     connection pooler (PgBouncer, or Supavisor on Supabase) or a smaller pool**.
   - The pool is now configurable: `DB_POOL_SIZE` (5) and `DB_MAX_OVERFLOW` (10). Keep
     replicas × (size + overflow) below `max_connections`.
5. **Memory is modest and stable:** under 210 MB per process throughout.
6. **Crash recovery time is the lease.** Documents on a killed process wait
   `INGESTION_JOB_LEASE_SECONDS` (600 s by default; 30 s in this test) before another
   process resumes them. A graceful stop hands jobs back at once (Phase 5.6). Keep the
   default lease unless crashes are frequent; it protects slow, large documents.

## Capacity guidance (estimates from these results, not guarantees)

For a **private beta** (tens of concurrent users), 2 API replicas with 1 to 2 vCPU each are
ample. Roughly:

- one replica sustains about 60 searches/s, or about 18 concurrent streams, with a p95 under
  about 1.5 s on laptop-class CPU;
- answers are bounded by OpenAI's latency and the tenant rate limits (10 answers per user per
  minute, 60 per workspace per minute, 3 concurrent per user, 10 per workspace) long before
  CPU.

For a **public launch**:

- load-test on the real platform at about 2× the expected peak;
- add a connection pooler past 6 replicas;
- **request higher OpenAI rate limits** (tokens per minute) for the expected answer volume.
  OpenAI's own limits were not part of this test, by design.

## What was not tested

- **Real OpenAI and real Supabase under load.** This was deliberate: there was to be no
  uncontrolled provider load. The real-provider behaviour checked here is per request
  (model evaluation, end-to-end scripts), not throughput.
- **Large files:** PDF and DOCX files near the 25 MB limit, and documents with thousands of
  chunks. These test extraction CPU and memory more than these 4 KB text files do.
- **Long durations:** soak tests for leaks over hours.
- **Load through a reverse proxy or TLS,** and from many client IP addresses.

## Remaining risks

- **Connection budget:** each replica can open up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` (15)
  connections. Size this per platform, or add a pooler, before scaling past a few replicas.
- **The numbers come from one laptop.** Repeat the test on staging hardware before a public
  launch.
- **The load test is run by hand.** Add it to a weekly or pre-release job against staging.
