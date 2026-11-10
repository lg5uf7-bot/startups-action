# Startup Companies + Jobs Collector

Daily, idempotent pipeline that discovers startups, enriches company profiles,
detects their ATS, and keeps active jobs in Supabase.

**Stack:** Python 3.13 · GitHub Actions · Supabase · Free APIs only · No VPS

## Repository structure

    startups-action/
    ├── .github/
    │   └── workflows/
    │       └── daily-sync.yml    # Daily at 02:00 UTC; manual dispatch
    ├── collector/                # Active Python pipeline
    │   ├── __init__.py
    │   ├── __main__.py           # python -m collector entry point
    │   ├── config.py             # Env-driven settings & budgets
    │   ├── db.py                 # Supabase/Postgres access + idempotent writes
    │   ├── http.py               # Polite HTTP client (retries, per-host spacing)
    │   ├── main.py               # CLI + run lifecycle + phase orchestration
    │   ├── models.py             # Dataclasses (Candidate, CompanyRecord, JobRecord)
    │   ├── normalize.py          # Domains, dates, money, HTML, remote/employment types
    │   ├── scoring.py            # 0–100 publishability score
    │   ├── ats/
    │   │   ├── __init__.py
    │   │   ├── adapters.py       # Greenhouse, Lever, Ashby, Workable, SmartRecruiters
    │   │   ├── detect.py         # Careers-page / link / slug-guess detection
    │   │   └── patterns.py       # ATS URL regexes + board URL builders
    │   ├── pipeline/
    │   │   ├── __init__.py
    │   │   ├── discovery.py      # Source ingestion → candidates table
    │   │   ├── enrich.py         # StartupDB enrichment + ATS detection + upsert
    │   │   ├── jobs.py           # Board fetch → upsert jobs + close missing
    │   │   ├── refresh.py        # Activity-tiered re-sync of existing boards
    │   │   └── report.py         # Daily run report → Telegram / webhook
    │   └── sources/
    │       ├── __init__.py
    │       ├── feeds.py          # Open Jobs catalog, Open ATS feed, Techstars feed
    │       ├── hn.py             # Hacker News "Who is hiring?"
    │       ├── remoteok.py       # RemoteOK
    │       ├── remotive.py       # Remotive
    │       ├── startupdb.py      # StartupDB enrichment (name/domain lookup only)
    │       ├── wwr.py            # We Work Remotely
    │       └── yc.py             # Y Combinator companies
    ├── .gitignore
    └── requirements.txt          # psycopg[binary], requests, beautifulsoup4, tldextract, rapidfuzz, python-dateutil

## Why this design

| Goal | How it is achieved |
| --- | --- |
| No VPS, no paid APIs | GitHub Actions (free minutes) + Supabase (free tier) + free public APIs |
| 6–10 good companies / day | Bounded enrichment (25/day), job sync (25/new), refresh (120/existing) |
| Broad discovery, no niche | HN, YC, RemoteOK, Remotive, WWR, Open Jobs catalog, Open ATS feed, Techstars feed |
| Never lose data | Idempotent upserts, company_data snapshot history, ats_sources stores every board, jobs.history keeps open/update/close events |
| Clean for frontend / API | Only is_active AND is_public companies and available jobs are readable |
| Separate gates | Company quality (score ≥ 30) is separate from job count; zero jobs never rejects a good company |
| Soft deletes | Jobs go status = unavailable + unavailable_on, never hard-deleted |
| Review queue | Permanent failures go to reviews; transient failures retry via candidates |
| Tiered refresh | Companies with jobs every 3 days, without jobs every 14 days |

## Pipeline flow

    DISCOVERY  (candidates, 8 sources)
          │
          ▼
    ENRICHMENT  (StartupDB + ATS detection)
          │
          ▼
    JOBS  (upsert jobs + close missing)
          │
          ▼
    REFRESH  (activity-tiered re-sync of existing boards)
          │
          ▼
    REPORT  (Telegram / webhook)

Data flow: discovery upserts into candidates only → enrichment scores and upserts into companies → jobs syncs boards into jobs → refresh re-syncs existing boards by activity → report writes runs and notifies.

## Data model (Supabase)

Exactly 5 tables:

| Table | Purpose |
| --- | --- |
| companies | Profile, ATS, funding, quality score, job counts, raw snapshot history |
| jobs | Active / closed jobs, description, salary, remote flag, full-text search, history |
| candidates | Discovery queue: source, ATS hint, attempts, raw payload |
| runs | One row per run: counters, phase timings, per-source items, logs, GitHub metadata |
| reviews | Permanent failures / manual-review cases |

Frontend-readable rows (via RLS): only companies where is_active = true AND is_public = true, joined to jobs where status = available. The collector uses the service-role connection string and bypasses RLS.

## Environment secrets

Reuses the repository's existing secrets — no new secret is required:

| Secret | Required | Purpose |
| --- | --- | --- |
| SIM_DATABASE_URL | Yes | Supabase Postgres connection string (also accepts SUPABASE_DB_URL / DATABASE_URL) |
| TELEGRAM_BOT_TOKEN | No | Daily Telegram summary |
| TELEGRAM_CHAT_ID | No | Telegram chat target (falls back to existing hardcoded ID if unset) |
| ALERT_WEBHOOK_URL | No | Optional failure webhook |

## Configuration (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| SIM_DATABASE_URL | — | Database URL (required) |
| MAX_ENRICH | 25 | Candidates to enrich per run |
| MAX_NEW_JOBS | 25 | New companies to sync jobs for |
| MAX_REFRESH | 120 | Existing companies to refresh |
| MAX_RUNTIME_SECONDS | 2700 | Hard runtime budget per run |
| MIN_QUALITY_SCORE | 30 | Score threshold to publish a company |
| HTTP_DELAY | 1.0 | Seconds between requests to the same host |
| STARTUPDB_DELAY | 0.55 | Seconds between StartupDB calls |
| DRY_RUN | false | Fetch sources only, no DB writes |

## Quality rules (publish threshold = 30)

A company is published when its score ≥ MIN_QUALITY_SCORE (default 30):

- Usable name: 5
- Website: 15
- Description: 15
- Founded year: 5
- Headquarters: 5
- Logo: 5
- Industries: 5
- Funding (round or amount): 15
- Total funding USD: 10
- Verified ATS board: 25

An ATS-only company with a verified board scores 30 and is still published — its jobs are useful even with thin marketing metadata.

Hard rejects (go to reviews, never published):

- No usable company name
- No website and no ATS hint
- Score below threshold

Transient failures (network, rate limits) stay in candidates and retry; only permanent failures go to reviews.

## ATS support

| Provider | Job API | Detection method |
| --- | --- | --- |
| Greenhouse | boards-api.greenhouse.io | Link, careers page, slug guess |
| Lever | api.lever.co / api.eu.lever.co | Link, careers page, slug guess |
| Ashby | posting-api.ashbyhq.com | Link, careers page, slug guess |
| Workable | apply.workable.com/api/v1/widget | Link |
| SmartRecruiters | api.smartrecruiters.com/v1 | Link |

Detection order: explicit discovery link → careers page / iframe scan → bounded slug guesses. Every candidate board is probed before it is stored.

## Data sources & attribution

All free, no API keys:

- StartupDB — enrichment only (name/domain lookup, not industry search). Data licensed CC BY 4.0. Attribution stored in raw snapshots.
- Hacker News — Algolia API ("Who is hiring?" threads)
- Y Combinator — public company API
- RemoteOK / Remotive / We Work Remotely — public job feeds (discovery only; jobs stored from the company's ATS)
- Open Jobs catalog — 378 name → ATS token mappings
- Open ATS feed — ~11k live Greenhouse / Lever / Ashby board tokens
- Techstars ecosystem feed — ~15k accelerator companies

The HTTP client spaces requests per host and retries 429 / 5xx with backoff.

## Operations

- Schedule: .github/workflows/daily-sync.yml runs at 02:00 UTC daily. Can also be triggered manually via workflow_dispatch with mode, max_enrich, max_new, max_refresh, dry_run.
- Locking: A Postgres advisory lock prevents overlapping runs.
- Runtime budget: 2700 seconds (~45 min) per run; refresh stops early if the deadline approaches.
- Stuck candidates: processing candidates older than 2 hours are reset to retry.
- Legacy: The old root-level modules (ai_researcher.py, discovery.py, engine.py, net.py, providers.py, sync_daily.py) have been removed. The active pipeline is collector/.

## Local run

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

    export SIM_DATABASE_URL="postgresql://..."
    python -m collector --mode daily

Dry run (no database):

    python -m collector --dry-run

Backfill beyond daily quota:

    python -m collector --mode backfill --max-enrich 200 --max-new 200 --max-refresh 0