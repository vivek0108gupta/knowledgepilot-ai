# Phase 5.7: security review, threat model and checklist

**This is an internal review, not an external audit or penetration test.** It was done by
reading the code, checking it with automated tests, and probing a running instance. Before a
public launch with sensitive customer data, commission an independent penetration test (see
"Remaining risks").

Each item states its evidence:

- **Confirmed** means verified by an automated test or a check run on 2026-11-12;
- **Reviewed** means the code was read but there is no dedicated test;
- **Assumption** means it depends on deployment configuration not present here.

## System and trust boundaries

```
Browser ──TLS──> Web app (Next.js)            no secrets; Supabase anon key only
   │                                          
   └──TLS + Bearer JWT──> API (FastAPI) ──> PostgreSQL (private network, pgvector)
                           │  │
                           │  ├──> Supabase Storage (secret key; one private bucket)
                           │  └──> OpenAI (API key; store=false)
                           └── /metrics (operator token; not the user API)
Supabase Auth issues ES256 JWTs; the API verifies them against Supabase's JWKS.
```

**Assets:**

- tenants' documents: files, extracted text, chunks and embeddings;
- conversations;
- usage and cost data;
- the OpenAI and Supabase secret keys;
- the database credentials;
- the backup encryption key;
- the metrics token.

## Threat model (STRIDE)

| Threat | Example | Mitigation | Evidence |
|---|---|---|---|
| **Spoofing** | Forged or `alg=none` / HS256 token; a token from another Supabase project | ES256/RS256 only; JWKS from the configured project; `exp`, `sub`, `iss`, `aud` required; 10 s leeway | Confirmed (`tests/test_auth.py`) |
| | Using a user's token on `/metrics` | Separate operator token, compared in constant time; 404 when unset | Confirmed (`test_observability.py`) |
| **Tampering** | Reading or changing another workspace's documents, conversations or quotas by ID | Every query is scoped by workspace membership. Outsiders get 404, not 403, so IDs cannot be probed. Isolation holds even if the first membership check were skipped. | Confirmed (isolation tests in every area, for example `test_another_workspaces_document_is_never_streamed_or_cited`, `test_isolation_holds_even_if_the_first_membership_check_were_skipped`) |
| | SQL injection | SQLAlchemy with bound parameters throughout `app/`; no string-built SQL outside the operator tools, whose identifiers come from the catalogue or are generated, and are quoted | Reviewed (grep of `app/` for `text(f"`, `execute(f"`, `.format(`) |
| | Tampered backups | AES-256-GCM per frame, position-bound | Confirmed (`test_backups.py`; live tamper and wrong-key checks) |
| | Members raising their own quotas | Owners only, tightening only, audited | Confirmed (Phase 5.5 and 5.6 tests) |
| **Repudiation** | "I didn't change that limit" | `audit_events` with before and after values; request IDs in logs | Confirmed |
| **Information disclosure** | Content, prompts, keys, JWTs or signed URLs in logs | Logs carry IDs, counts and error types only. SQL parameters are hidden (`hide_parameters`). httpx and OpenAI loggers are at WARNING, because storage URLs contain file names. | Confirmed (log tests; about 7,000 lines in the resilience run and about 6,600 in the load test scanned for keys, tokens, `Bearer` and document text: none) |
| | Error bodies leaking internals | A generic 500 with no traceback; the cause goes only to the logs | Confirmed (`test_production_readiness.py`, resilience run 12) |
| | API schema in production | `/docs`, `/redoc` and `/openapi.json` are off when `ENVIRONMENT=production` | Confirmed |
| | Signed download URLs | 60 s TTL; `download` forces saving, not rendering; issued only after a membership check; never logged | Confirmed (Phase 3 tests) |
| | Public bucket | The API refuses to use a bucket that is not private | Confirmed |
| | Conversations kept by OpenAI | `store: false` on every request | Confirmed (`test_llm.py`) |
| | Operational data shown to tenants | Metrics are not reachable with a user session; the usage pages show only the tenant's own usage | Confirmed |
| | Browser caching tenant data | `Cache-Control: no-store` on API responses | Confirmed (`test_security_headers.py`) |
| **Denial of service and cost abuse** | Huge request bodies | `MAX_REQUEST_BODY_BYTES` (1 MiB; 413 or 411) before authentication; uploads limited to 25 MB while streaming | Confirmed |
| | Flooding chat, search or uploads | Per-user and per-workspace rate limits, shared across replicas through the database | Confirmed, **also under load**: 45 searches from one user across both processes gave exactly 30 × 200 and 15 × 429 ([load testing](phase-5.7-load-testing.md)) |
| | Running up the OpenAI bill | Reservations before every call, daily, monthly and per-workspace token and cost limits, concurrency limits | Confirmed, **also under load**: 9 concurrent answers against a limit; the workspace was never charged past it |
| | Slow-loris or stuck provider | Timeouts on every provider call; stream deadline; bounded retries | Confirmed (resilience runs 17 and 18) |
| **Elevation of privilege** | A member acting as an admin or owner | Role checks per action | Confirmed (`test_workspaces.py`, `test_usage*`) |
| | **Prompt injection in an uploaded document** | Passages are fenced with a random per-request boundary and labelled as untrusted data; the instructions say never to follow them; citations are verified against the passages; the system prompt is detected if leaked; the model has **no tools**, so injected text cannot act, only influence wording | Confirmed (`test_rag_components.py`, `test_chat.py`). The real-model evaluation included an injected "ignore your instructions" passage, and **none of the five model configurations followed it** ([model evaluation](phase-5.7-model-evaluation.md)). |
| | Malicious upload (scripts in HTML or SVG, zip bombs, wrong type) | Allowed types checked by content, not by extension; text extraction only, nothing is executed; downloads are forced as attachments | Confirmed (Phase 3 file-validation tests) |

## Security review checklist

`[x]` means done and confirmed in this repository. `[ ]` means it is a deployment task or
still open.

### Authentication and authorisation
- [x] Every user route requires a verified Supabase JWT; health and metrics are the only
  exceptions, and metrics has its own token.
- [x] Asymmetric algorithms only; issuer, audience and expiry are required.
- [x] Workspace isolation on every read and write. Outsiders get 404.
- [x] Role checks (owner, admin, member) on management actions.
- [ ] **Deployment:** Supabase Auth settings. Confirm the email, MFA and password policy, and
  turn off sign-ups if the beta is invite-only.

### Input, uploads and storage
- [x] Request body limit before authentication; upload size limit while streaming.
- [x] File type validated by content; extraction only.
- [x] Private bucket enforced; paths are server-generated (`<workspace>/<document>/…`) and a
  database constraint binds them to the owning workspace.
- [x] Signed URLs are short-lived, forced to download, and never logged.
- [x] Pydantic validation on every body (`extra="forbid"` where it matters).

### AI-specific
- [x] Prompt injection mitigations (see above). The model has no tools or actions.
- [x] Citations are verified against retrieved passages; unverifiable ones are dropped.
- [x] `store: false`; no conversation is kept by OpenAI.
- [x] Model parameters per model (no silent failure after a model change); fallback only on
  "model not found".

### Secrets
- [x] No secrets in the repository: `.env` is gitignored; `.env.example` holds placeholders.
  Confirmed on 2026-11-12 by scanning tracked and untracked files for key patterns
  (`sk-…`, `sb_secret_…`, long `eyJ…` JWTs). The only matches were deliberately fake values:
  `sk-test-key-never-real-…` in two test files, and `sk-load-test-local-only` in the load
  test. `.env` is ignored (`git check-ignore`).
- [x] Secrets are never logged (tests and scans above).
- [x] The web bundle holds only `NEXT_PUBLIC_*` values (the API URL, the Supabase URL and the
  anon key).
- [ ] **Deployment:** secrets in the platform's secret store; separate values for staging and
  production; a rotation procedure ([deployment runbook](phase-5.7-deployment-runbook.md#secrets)).

### Abuse and cost
- [x] Rate limits and quotas, consistent across replicas, checked under load.
- [x] Concurrency limits per user and per workspace.
- [x] Usage ledger with reservations; nothing is charged twice; abandoned operations are
  recovered.

### Transport, CORS and headers
- [x] CORS: an explicit origin list, credentials allowed only for those origins, explicit
  methods and headers. The deployment checklist forbids wildcards.
- [x] API headers on every response, errors included:
  - `X-Content-Type-Options: nosniff`;
  - `X-Frame-Options: DENY`;
  - `Referrer-Policy: no-referrer`;
  - `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (except the
    development-only docs pages);
  - `Cache-Control: no-store` (routes that set their own value keep it);
  - `Cross-Origin-Resource-Policy: same-site`;
  - `Permissions-Policy`;
  - `Strict-Transport-Security` in production.
- [x] Web headers on every page:
  - CSP `frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'`;
  - `X-Frame-Options`, `nosniff`, `Referrer-Policy` and `Permissions-Policy`;
  - HSTS;
  - no `X-Powered-By`.
- [ ] **Open:** a full `script-src` CSP for the web app. It needs per-request nonces in
  Next.js; without them, injected script would run if an XSS bug existed. React escapes
  output, and `dangerouslySetInnerHTML` is not used anywhere in the web app (confirmed by
  search).
- [ ] **Deployment:** TLS everywhere (assumption); the database not reachable from the
  internet (assumption; Compose binds it to 127.0.0.1).

### Errors and logging
- [x] Generic error bodies; causes only in the logs; request IDs for correlation.
- [x] JSON logs available (`LOG_FORMAT=json`); no content or secrets in them.

### Dependencies (checked 2026-11-12)
- [x] Python: `pip-audit` over the locked dependencies (`uv export --frozen`) found **no known
  vulnerabilities**.
- [x] JavaScript production dependencies: `npm audit --omit=dev` found **0
  vulnerabilities**.
- [ ] JavaScript dev dependencies: **8 moderate** findings, all one advisory in `minimatch`
  under the `typescript-eslint` / `eslint-config-next` lint toolchain. It is not shipped in
  the web image and only runs on developer machines and in CI. The fix needs a breaking
  upgrade (`npm audit fix --force`); do that when `eslint-config-next` publishes a compatible
  release.
- [ ] Automate: run `pip-audit` and `npm audit --omit=dev` in CI and fail on high or
  critical findings.

### Operations
- [x] Encrypted, verified backups, with a restore drill ([backup strategy](phase-5.7-backup-strategy.md)).
- [x] An operator-only metrics endpoint ([observability](phase-5.7-observability.md)).
- [ ] **Deployment:** error tracking with data scrubbing, alerting, and log retention and
  access control.

## Findings from this phase

| # | Severity | Finding | Status |
|---|---|---|---|
| S1 | Medium (availability) | Switching to either luna model would have failed every answer: `temperature` was always sent | **Fixed**: per-model parameters ([model evaluation](phase-5.7-model-evaluation.md)) |
| S2 | Low | The API sent no hardening headers, and the web app none either (clickjacking, MIME sniffing, caching of tenant JSON) | **Fixed**, with tests |
| S3 | Low | No backup of Storage files existed; Supabase's backups do not include them | **Fixed**: `storage_backup` |
| S4 | Info | Dev-only `minimatch` advisory | Open; not shipped |
| S5 | Info | The web CSP does not restrict scripts | Open; needs nonce support |

## Remaining risks (not mitigated here)

1. **No external penetration test** has been done. Recommended before a public launch;
   scope it to the API (authentication, isolation, uploads), prompt injection and the web
   app.
2. **Prompt injection is mitigated, not solved.** A crafted document could still bias how an
   answer is worded for the users of **its own** workspace. It cannot reach other
   workspaces, call tools or exfiltrate data through actions. Answers always show their
   sources.
3. **No malware scanning of uploads.** Files are only parsed for text, never executed or
   served inline, but a downloaded file could still be malicious for the user who opens it.
   Consider a scanning step for a public launch.
4. **Account security depends on Supabase Auth settings** (MFA, password policy, sign-up
   control). These are not visible from this repository.
5. **No web application firewall or bot protection.** Rate limits are per user, so many
   accounts created by one actor could combine their quotas. Mitigation: control sign-ups,
   or add CAPTCHA and email verification in Supabase.
6. **Deletion does not reach backups** until they age out (30 days). State this in the
   privacy policy.
