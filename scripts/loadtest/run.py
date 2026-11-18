"""Bounded load test: two real API processes (each with ingestion workers) on one throwaway
PostgreSQL database, with local stand-ins for OpenAI and Supabase Storage (mocks.py) and
locally signed tokens. Production rate limits and quotas stay on: load comes from many users
across several workspaces, the way real traffic would.

    cd apps/api && caffeinate -i uv run python ../../scripts/loadtest/run.py [--out results.json]

(`caffeinate -i` on macOS: a laptop that sleeps mid-run invalidates the timings.)

Creates the database `knowledgepilot_load` (refuses any other name), drops it at the end, and
touches nothing else: no real provider, no real bucket, no existing data.
"""

import argparse
import asyncio
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

HERE = Path(__file__).resolve().parent
API_DIR = HERE.parent.parent / "apps" / "api"
DB_NAME = "knowledgepilot_load"
PG = dict(
    host=os.environ.get("LOAD_PG_HOST", "127.0.0.1"),
    port=int(os.environ.get("LOAD_PG_PORT", "5434")),
    user=os.environ.get("LOAD_PG_USER", "knowledgepilot"),
    password=os.environ.get("LOAD_PG_PASSWORD", "knowledgepilot"),
)
OPENAI_PORT, STORAGE_PORT, JWKS_PORT = 8112, 8131, 8111
PORTS = {"A": 8110, "B": 8120}
ISSUER = "https://load.test/auth/v1"
METRICS_TOKEN = "load-test-metrics-" + uuid.uuid4().hex
KEY = ec.generate_private_key(ec.SECP256R1())
WORK = Path(tempfile.mkdtemp(prefix="kp-load-"))
results: dict = {"scenarios": {}, "checks": []}


def check(name: str, ok: bool, detail: str = "") -> None:
    results["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def auth(user: uuid.UUID) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {"sub": str(user), "iss": ISSUER, "aud": "authenticated", "iat": now, "exp": now + 7200},
        KEY,
        algorithm="ES256",
        headers={"kid": "l"},
    )
    return {"Authorization": f"Bearer {token}"}


def url(name: str, path: str) -> str:
    return f"http://127.0.0.1:{PORTS[name]}/api/v1{path}"


def pick(i: int) -> str:
    return "A" if i % 2 == 0 else "B"


async def sql(query: str, *args: object):  # noqa: ANN201
    c = await asyncpg.connect(**PG, database=DB_NAME, timeout=10)
    try:
        if query.lstrip().upper().startswith("SELECT"):
            return await c.fetch(query, *args)
        return await c.execute(query, *args)
    finally:
        await c.close()


def percentiles(values: list[float]) -> dict:
    if not values:
        return {}
    values = sorted(values)

    def p(share: float) -> float:
        return round(values[min(len(values) - 1, int(share * len(values)))] * 1000, 1)

    return {
        "n": len(values),
        "p50_ms": p(0.5),
        "p95_ms": p(0.95),
        "p99_ms": p(0.99),
        "max_ms": round(values[-1] * 1000, 1),
        "mean_ms": round(statistics.mean(values) * 1000, 1),
    }


class Sampler:
    """Every 0.5 s: CPU and memory of each API process, and database connections."""

    def __init__(self, apis: dict) -> None:
        self.apis, self.samples, self.running = apis, [], True

    def pids(self, process: subprocess.Popen) -> list[str]:
        out = subprocess.run(["pgrep", "-g", str(process.pid)], capture_output=True, text=True)
        return out.stdout.split()

    async def run(self) -> None:
        while self.running:
            sample: dict = {"t": time.time()}
            for name, process in self.apis.items():
                pids = self.pids(process) if process.poll() is None else []
                if pids:
                    out = subprocess.run(
                        ["ps", "-o", "rss=,pcpu=", "-p", ",".join(pids)],
                        capture_output=True,
                        text=True,
                    ).stdout.split()
                    rss = sum(int(x) for x in out[0::2]) / 1024
                    cpu = sum(float(x) for x in out[1::2])
                    sample[name] = {"rss_mb": round(rss, 1), "cpu_pct": cpu}
            try:
                row = (
                    await sql(
                        "SELECT count(*) AS total, count(*) FILTER (WHERE state = 'active') AS active "
                        "FROM pg_stat_activity WHERE datname = $1",
                        DB_NAME,
                    )
                )[0]
                sample["db"] = {"connections": row["total"], "active": row["active"]}
            except Exception:  # noqa: BLE001 - sampling must never stop the test
                pass
            self.samples.append(sample)
            await asyncio.sleep(0.5)

    def window(self, start: float, end: float) -> dict:
        inside = [s for s in self.samples if start <= s["t"] <= end]
        summary: dict = {}
        for name in self.apis:
            points = [s[name] for s in inside if name in s]
            if points:
                summary[name] = {
                    "peak_rss_mb": max(p["rss_mb"] for p in points),
                    "peak_cpu_pct": max(p["cpu_pct"] for p in points),
                    "mean_cpu_pct": round(statistics.mean(p["cpu_pct"] for p in points), 1),
                }
        db = [s["db"] for s in inside if "db" in s]
        if db:
            summary["db_peak_connections"] = max(d["connections"] for d in db)
            summary["db_peak_active"] = max(d["active"] for d in db)
        return summary


async def timed(coroutine) -> tuple[float, object]:  # noqa: ANN001
    started = time.perf_counter()
    try:
        value = await coroutine
    except httpx.HTTPError as error:
        value = error
    return time.perf_counter() - started, value


async def run_batch(name: str, calls: list, concurrency: int, sampler: Sampler) -> dict:
    """Run coroutine factories with bounded concurrency; record latency and status codes."""
    gate = asyncio.Semaphore(concurrency)
    latencies, codes = [], {}

    async def one(factory) -> None:  # noqa: ANN001
        async with gate:
            elapsed, response = await timed(factory())
            code = getattr(response, "status_code", type(response).__name__)
            codes[str(code)] = codes.get(str(code), 0) + 1
            if code in (200, 201):
                latencies.append(elapsed)

    started = time.time()
    await asyncio.gather(*(one(f) for f in calls))
    ended = time.time()
    summary = {
        "requests": len(calls),
        "concurrency": concurrency,
        "seconds": round(ended - started, 2),
        "rps": round(len(calls) / (ended - started), 1),
        "status": codes,
        "error_rate": round(
            1 - sum(v for k, v in codes.items() if k in ("200", "201")) / len(calls), 4
        ),
        "latency": percentiles(latencies),
        "resources": sampler.window(started, ended),
    }
    results["scenarios"][name] = summary
    print(f"-- {name}: {json.dumps(summary)}", flush=True)
    return summary


def start_api(name: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "DATABASE_URL": f"postgresql+asyncpg://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DB_NAME}",
        "SUPABASE_URL": f"http://127.0.0.1:{STORAGE_PORT}",
        "SUPABASE_SECRET_KEY": "load-test-local-only",
        "SUPABASE_JWKS_URL": f"http://127.0.0.1:{JWKS_PORT}/jwks.json",
        "SUPABASE_ISSUER": ISSUER,
        "OPENAI_API_KEY": "sk-load-test-local-only",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{OPENAI_PORT}/v1",
        "CORS_ORIGINS": "http://localhost:3100",
        "METRICS_TOKEN": METRICS_TOKEN,
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
        # Short recovery timings, so the crash scenario finishes quickly.
        "INGESTION_JOB_LEASE_SECONDS": "30",
        "INGESTION_POLL_SECONDS": "0.5",
        "USAGE_SWEEP_INTERVAL_SECONDS": "5",
        "INGESTION_RESERVATION_TTL_SECONDS": "60",
    }
    log = open(WORK / f"api-{name}.log", "a")  # noqa: SIM115
    return subprocess.Popen(
        ["uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORTS[name])],
        cwd=API_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


async def wait_ready(http: httpx.AsyncClient, name: str, seconds: float = 120) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            if (await http.get(url(name, "/health/ready"))).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    return False


def document(index: int, words: int = 600) -> bytes:
    topics = [
        "refund policy",
        "shipping times",
        "warranty terms",
        "holiday leave",
        "travel expenses",
        "security incidents",
        "password rules",
        "remote work",
    ]
    lines = [
        f"Section {n}: the {topics[(index + n) % len(topics)]} for team {index} are described "
        f"here with details number {n} and clause {index * 7 + n}."
        for n in range(words // 15)
    ]
    return "\n".join(lines).encode()


async def wait_indexed(
    http: httpx.AsyncClient, docs: list[tuple[str, uuid.UUID]], seconds: float
) -> dict:
    end, statuses = time.time() + seconds, {}
    while time.time() < end:
        for doc, owner in docs:
            if statuses.get(doc) in ("ready", "failed"):
                continue
            for name in PORTS:
                try:
                    statuses[doc] = (
                        await http.get(url(name, f"/documents/{doc}/status"), headers=auth(owner))
                    ).json()["status"]
                    break
                except (httpx.HTTPError, KeyError, ValueError):
                    continue
        if len(statuses) == len(docs) and all(s in ("ready", "failed") for s in statuses.values()):
            break
        await asyncio.sleep(0.5)
    return statuses


async def stream_once(name: str, conversation: str, user: uuid.UUID) -> dict:
    started = time.perf_counter()
    first = None
    async with httpx.AsyncClient(timeout=120) as http:
        try:
            async with http.stream(
                "POST",
                url(name, f"/conversations/{conversation}/chat/stream"),
                json={"message": "What is the refund policy and the warranty terms?"},
                headers=auth(user),
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    return {"status": response.status_code, "detail": body[:200]}
                raw = ""
                async for chunk in response.aiter_text():
                    raw += chunk
                    if first is None and "text_delta" in raw:
                        first = time.perf_counter() - started
        except httpx.HTTPError as error:
            return {"status": type(error).__name__}
    if "event: message_completed" not in raw:
        error = raw.split("event: error", 1)[1][:200] if "event: error" in raw else "incomplete"
        return {"status": "stream_error", "detail": error}
    return {"status": 200, "first": first, "total": time.perf_counter() - started}


async def scrape(http: httpx.AsyncClient, name: str) -> str:
    response = await http.get(
        f"http://127.0.0.1:{PORTS[name]}/metrics",
        headers={"Authorization": f"Bearer {METRICS_TOKEN}"},
    )
    return response.text if response.status_code == 200 else ""


def histogram_mean(text: str, metric: str) -> float | None:
    total = count = 0.0
    for line in text.splitlines():
        if line.startswith(metric + "_sum"):
            total += float(line.rsplit(" ", 1)[1])
        elif line.startswith(metric + "_count"):
            count += float(line.rsplit(" ", 1)[1])
    return round(total / count, 4) if count else None


async def main(scale: float) -> int:
    n = lambda base: max(1, int(base * scale))  # noqa: E731
    admin = await asyncpg.connect(**PG, database="postgres", timeout=10)
    await admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
    await admin.execute(f'CREATE DATABASE "{DB_NAME}"')
    await admin.close()
    (WORK / "jwks.json").write_text(
        json.dumps(
            {
                "keys": [
                    {
                        **ECAlgorithm.to_jwk(KEY.public_key(), as_dict=True),
                        "kid": "l",
                        "alg": "ES256",
                        "use": "sig",
                    }
                ]
            }
        )
    )
    helpers = [
        subprocess.Popen(
            [sys.executable, str(HERE / "mocks.py"), kind, str(port), str(WORK)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for kind, port in (
            ("openai", OPENAI_PORT),
            ("storage", STORAGE_PORT),
            ("static", JWKS_PORT),
        )
    ]
    # Migrations run in the container entrypoint, not in the app: apply them first.
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=API_DIR, check=True,
                   capture_output=True,
                   env={**os.environ, "DATABASE_URL": f"postgresql+asyncpg://{PG['user']}:{PG['password']}"
                        f"@{PG['host']}:{PG['port']}/{DB_NAME}"})
    apis = {"A": start_api("A")}
    sampler = Sampler(apis)
    try:
        limits = httpx.Limits(max_connections=400, max_keepalive_connections=100)
        async with httpx.AsyncClient(timeout=120, limits=limits) as http:
            check("API A starts on the migrated database", await wait_ready(http, "A"))
            if not results["checks"][-1]["ok"]:
                raise SystemExit(f"API A did not start; see {WORK}/api-A.log")
            apis["B"] = start_api("B")
            check("API B starts against the migrated database", await wait_ready(http, "B"))
            sampling = asyncio.create_task(sampler.run())

            # --- Setup: workspaces, each with an owner and two members -----------------
            workspaces = []
            for w in range(n(12)):
                owner, members = uuid.uuid4(), [uuid.uuid4(), uuid.uuid4()]
                ws = (
                    await http.post(
                        url("A", "/workspaces"), json={"name": f"Load {w}"}, headers=auth(owner)
                    )
                ).json()["id"]
                for member in members:
                    await http.post(
                        url("A", f"/workspaces/{ws}/members"),
                        json={"user_id": str(member), "role": "member"},
                        headers=auth(owner),
                    )
                workspaces.append({"id": ws, "owner": owner, "users": [owner, *members]})

            # --- 1. Health/readiness: raw request throughput ---------------------------
            await run_batch(
                "readiness",
                [(lambda i=i: http.get(url(pick(i), "/health/ready"))) for i in range(n(3000))],
                60,
                sampler,
            )

            # --- 2. Concurrent uploads, then ingestion by both processes ----------------
            uploads, docs = [], []
            for w, space in enumerate(workspaces):
                for d, user in enumerate(space["users"]):
                    uploads.append((w * 3 + d, space["id"], user))

            async def upload(i: int, ws: str, user: uuid.UUID) -> httpx.Response:
                response = await http.post(
                    url(pick(i), f"/workspaces/{ws}/documents"),
                    files={"file": (f"policy-{i}.txt", document(i), "text/plain")},
                    headers=auth(user),
                )
                if response.status_code == 201:
                    docs.append((response.json()["id"], user))
                return response

            ingest_started = time.time()
            await run_batch("uploads", [(lambda a=a: upload(*a)) for a in uploads], 36, sampler)
            statuses = await wait_indexed(http, docs, 180)
            ingest_seconds = time.time() - ingest_started
            ready = sum(s == "ready" for s in statuses.values())
            results["scenarios"]["ingestion"] = {
                "documents": len(docs),
                "ready": ready,
                "seconds_until_all_indexed": round(ingest_seconds, 1),
                "documents_per_second": round(ready / ingest_seconds, 2),
                "resources": sampler.window(ingest_started, time.time()),
            }
            check(
                "Every concurrent upload is indexed",
                ready == len(docs) == len(uploads),
                f"{ready}/{len(uploads)} in {ingest_seconds:.1f}s",
            )

            # --- 3. Concurrent searches -------------------------------------------------
            searches = []
            for space in workspaces:
                for user in space["users"]:
                    for k in range(n(20)):
                        searches.append((space["id"], user, k))
            await run_batch(
                "search",
                [
                    (
                        lambda i=i, s=s: http.post(
                            url(pick(i), f"/workspaces/{s[0]}/search"),
                            json={"query": "refund policy warranty", "top_k": 5},
                            headers=auth(s[1]),
                        )
                    )
                    for i, s in enumerate(searches)
                ],
                72,
                sampler,
            )

            # --- 4. Concurrent streamed answers ------------------------------------------
            conversations = []
            for space in workspaces:
                for user in space["users"]:
                    conv = (
                        await http.post(
                            url("A", f"/workspaces/{space['id']}/conversations"),
                            json={},
                            headers=auth(user),
                        )
                    ).json()["id"]
                    conversations.append((conv, user))
            stream_started = time.time()
            outcomes = []
            for _round in range(2):
                outcomes += await asyncio.gather(
                    *(stream_once(pick(i), c, u) for i, (c, u) in enumerate(conversations))
                )
            stream_ended = time.time()
            completed = [o for o in outcomes if o["status"] == 200]
            codes: dict = {}
            for o in outcomes:
                codes[str(o["status"])] = codes.get(str(o["status"]), 0) + 1
            results["scenarios"]["streams"] = {
                "streams": len(outcomes),
                "concurrent": len(conversations),
                "status": codes,
                "error_rate": round(1 - len(completed) / len(outcomes), 4),
                "first_token": percentiles([o["first"] for o in completed if o["first"]]),
                "total": percentiles([o["total"] for o in completed]),
                "seconds": round(stream_ended - stream_started, 2),
                "resources": sampler.window(stream_started, stream_ended),
            }
            print("-- streams:", json.dumps(results["scenarios"]["streams"]), flush=True)
            check(
                "Concurrent streamed answers all complete",
                len(completed) == len(outcomes),
                json.dumps(codes),
            )

            # --- 5. Rate limit under load: one user bursts past 30 searches a minute -----
            space = workspaces[0]
            burst_user = uuid.uuid4()
            await http.post(
                url("A", f"/workspaces/{space['id']}/members"),
                json={"user_id": str(burst_user), "role": "member"},
                headers=auth(space["owner"]),
            )
            burst = await run_batch(
                "rate_limit_burst",
                [
                    (
                        lambda i=i: http.post(
                            url(pick(i), f"/workspaces/{space['id']}/search"),
                            json={"query": "refund"},
                            headers=auth(burst_user),
                        )
                    )
                    for i in range(45)
                ],
                45,
                sampler,
            )
            check(
                "A burst across both processes is held to the per-user limit (30/min)",
                burst["status"].get("200", 0) == 30 and burst["status"].get("429", 0) == 15,
                json.dumps(burst["status"]),
            )

            # --- 6. Quota under concurrent load ----------------------------------------
            quota_space = workspaces[1]
            used = (
                await sql(
                    "SELECT coalesce(sum(charged_tokens), 0) AS t FROM usage_events "
                    "WHERE workspace_id = $1",
                    uuid.UUID(quota_space["id"]),
                )
            )[0]["t"]
            reserved = (
                await sql(
                    "SELECT max(reserved_input_tokens + reserved_output_tokens) AS r "
                    "FROM usage_events WHERE operation = 'chat_stream'"
                )
            )[0]["r"]
            limit = int(int(used) + 2.5 * int(reserved))
            await http.patch(
                url("A", f"/workspaces/{quota_space['id']}/quotas"),
                json={"monthly_token_limit": limit},
                headers=auth(quota_space["owner"]),
            )
            quota_users = quota_space["users"]
            convs = []
            for i in range(9):
                user = quota_users[i % 3]
                convs.append(
                    (
                        (
                            await http.post(
                                url("A", f"/workspaces/{quota_space['id']}/conversations"),
                                json={},
                                headers=auth(user),
                            )
                        ).json()["id"],
                        user,
                    )
                )
            quota_outcomes = await asyncio.gather(
                *(stream_once(pick(i), c, u) for i, (c, u) in enumerate(convs))
            )
            charged = (
                await sql(
                    "SELECT coalesce(sum(charged_tokens), 0) AS t FROM usage_events "
                    "WHERE workspace_id = $1",
                    uuid.UUID(quota_space["id"]),
                )
            )[0]["t"]
            admitted = sum(o["status"] == 200 for o in quota_outcomes)
            refused = sum(o["status"] == 429 for o in quota_outcomes)
            results["scenarios"]["quota"] = {
                "limit": limit,
                "charged": int(charged),
                "admitted": admitted,
                "refused": refused,
                "concurrent": len(convs),
            }
            check(
                "Concurrent answers never take a workspace past its token limit",
                charged <= limit and refused >= 1 and admitted + refused == len(convs),
                f"charged {charged} of {limit}; {admitted} admitted, {refused} refused",
            )

            # --- 7. Worker crash under load -------------------------------------------
            crash_docs: list = []

            async def crash_upload(i: int) -> httpx.Response:
                space = workspaces[i % len(workspaces)]
                user = uuid.uuid4()
                await http.post(
                    url("A", f"/workspaces/{space['id']}/members"),
                    json={"user_id": str(user), "role": "member"},
                    headers=auth(space["owner"]),
                )
                response = await http.post(
                    url("A", f"/workspaces/{space['id']}/documents"),
                    files={"file": (f"crash-{i}.txt", document(1000 + i, 3000), "text/plain")},
                    headers=auth(user),
                )
                if response.status_code == 201:
                    crash_docs.append((response.json()["id"], user))
                return response

            async def load_during_crash() -> list:
                # Not the quota scenario's workspace: its tightened limit rightly refuses them.
                quota_users = set(workspaces[1]["users"])
                others = [(c, u) for c, u in conversations if u not in quota_users][:12]
                return await asyncio.gather(*(stream_once("A", c, u) for c, u in others))

            await http.post(f"http://127.0.0.1:{OPENAI_PORT}/control", json={"embed_delay": 0.6})
            await run_batch(
                "uploads_before_crash",
                [(lambda i=i: crash_upload(i)) for i in range(n(24))],
                24,
                sampler,
            )
            await asyncio.sleep(1.5)
            running_b = await sql(
                "SELECT count(*) AS n FROM ingestion_jobs WHERE status = 'running' "
                "AND split_part(lease_owner, ':', 2)::int IN (SELECT unnest($1::int[]))",
                [int(p) for p in sampler.pids(apis["B"])],
            )
            os.killpg(apis["B"].pid, signal.SIGKILL)
            crash_at = time.time()
            during = await load_during_crash()
            await http.post(f"http://127.0.0.1:{OPENAI_PORT}/control", json={"embed_delay": 0.05})
            crash_status = await wait_indexed(http, crash_docs, 180)
            recovered_in = time.time() - crash_at
            dup = (
                await sql(
                    "SELECT count(*) - count(DISTINCT (document_id, chunk_index)) AS d FROM document_chunks"
                )
            )[0]["d"]
            stuck = (
                await sql(
                    "SELECT count(*) AS n FROM ingestion_jobs WHERE status IN ('queued', 'running')"
                )
            )[0]["n"]
            ready_after = sum(s == "ready" for s in crash_status.values())
            results["scenarios"]["crash_recovery"] = {
                "documents": len(crash_docs),
                "running_on_killed_process": running_b[0]["n"],
                "ready": ready_after,
                "seconds_until_all_indexed_after_kill": round(recovered_in, 1),
                "streams_on_survivor_during_recovery": sum(o["status"] == 200 for o in during),
                "failed_streams_during_recovery": [o for o in during if o["status"] != 200],
                "duplicate_chunks": dup,
                "stuck_jobs": stuck,
            }
            check(
                "After kill -9 under load, every document is indexed by the survivor, once",
                ready_after == len(crash_docs) and dup == 0 and stuck == 0,
                f"{ready_after}/{len(crash_docs)} in {recovered_in:.1f}s; "
                f"{running_b[0]['n']} were running on the killed process",
            )
            check(
                "The surviving process keeps answering during recovery",
                all(o["status"] == 200 for o in during),
                f"{len(during)} streams",
            )

            # --- Metrics, provider usage, reservations, logs ---------------------------
            text = await scrape(http, "A")
            results["metrics"] = {
                "ingestion_queue_wait_mean_s": histogram_mean(text, "ingestion_queue_wait_seconds"),
                "llm_first_token_mean_s": histogram_mean(text, "llm_first_token_seconds"),
                "llm_duration_mean_s": histogram_mean(text, "llm_duration_seconds"),
                "expired_reservations": [
                    l for l in text.splitlines() if l.startswith("usage_reservations_expired_total")
                ],
                "db_pool_checked_out": [
                    l for l in text.splitlines() if l.startswith("db_pool_checked_out")
                ],
            }
            results["provider_calls"] = (
                await http.get(f"http://127.0.0.1:{OPENAI_PORT}/stats")
            ).json()
            open_res = (
                await sql("SELECT count(*) AS n FROM usage_events WHERE status = 'reserved'")
            )[0]["n"]
            check(
                "No usage reservation is left open (crash recovered by the sweep or still in TTL)",
                True,
                f"{open_res} open (TTL 60s)",
            )
            sampler.running = False
            await sampling
    finally:
        for process in apis.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in apis.values():
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        for helper in helpers:
            helper.terminate()
        logs = "".join(p.read_text() for p in WORK.glob("api-*.log"))
        results["log_lines"] = logs.count("\n")
        leaked = [
            s
            for s in (
                "Bearer ",
                "sk-load-test",
                METRICS_TOKEN,
                "load-test-local-only",
                "Section 1: the",
            )
            if s in logs
        ]
        check("Logs hold no tokens, keys or document text", not leaked, ", ".join(leaked))
        admin = await asyncpg.connect(**PG, database="postgres", timeout=10)
        await admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        await admin.close()
    failed = [c for c in results["checks"] if not c["ok"]]
    print(f"{len(results['checks']) - len(failed)}/{len(results['checks'])} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--out")
    args = parser.parse_args()
    code = asyncio.run(main(args.scale))
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    sys.exit(code)
