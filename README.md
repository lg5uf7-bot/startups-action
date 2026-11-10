# Startup Companies + Jobs Collector

Daily, idempotent data pipeline that discovers startups, enriches company profiles, detects their applicant tracking system (ATS), and keeps active jobs in Supabase.

The active implementation is the `collector/` package. The old root-level Python files are kept only for reference; the workflow no longer runs them.

## What it does

1. **Discovers** companies from several free sources:
   - Hacker News "Who is hiring?" threads
   - Y Combinator companies
   - RemoteOK, Remotive, and We Work Remotely
   - A 15k+ accelerator company feed (Techstars ecosystem)
   - Two community-maintained ATS catalogues with ~11k live Greenhouse, Lever, and Ashby boards
2. **Enriches** each company from StartupDB (name/domain lookup only), including description, founding year, HQ, logo, industries, and funding.
3. **Detects** the ATS by checking explicit job links, the company careers page, and a bounded set of slug guesses.
4. **Fetches jobs** from Greenhouse, Lever, Ashby, Workable, and SmartRecruiters.
5. **Stores everything** in Supabase with idempotent upserts, soft-closed jobs, raw payload snapshots, run tracking, and a review queue.
6. **Refreshes** companies by activity tier: companies with jobs every 3 days, companies without jobs every 14 days.

A normal day enriches 25 new companies, syncs jobs for the best new ones, and refreshes up to 120 existing boards. All limits are configurable.

## Repository layout

```
collector/
  config.py              environment + budgets
  db.py                  Supabase/Postgres access and idempotent writes
  http.py                polite HTTP client with retries and rate limits
  normalize.py           domains, dates, money, HTML, remote/employment types
  models.py              dataclasses
  scoring.py             publishability score
  main.py                CLI entry point
  sources/               HN, YC, RemoteOK, Remotive, WWR, feeds, StartupDB
  ats/                   ATS patterns, adapters, careers-page detection
  pipeline/              discovery, enrichment, jobs, refresh, reporting
schema.sql               additive Supabase migration
tests/                   offline unit tests
.github/workflows/       daily collector, CI, keepalive
```

## Supabase schema

The migration in `schema.sql` has been applied to the existing Supabase project. It is additive for `companies` and `jobs`, and it migrates the old `sync_logs` rows before removing the extra tables.

The final database has exactly **five tables**:

| Table | Purpose |
| --- | --- |
| `companies` | Company profile, ATS, funding, quality score, job counts, raw source snapshots |
| `jobs` | Active/closed jobs, descriptions, salary, remote flag, full-text search, history |
| `candidates` | Discovery queue with source, ATS hint, attempts, and raw payload |
| `runs` | One row per run with counters, phase timings, per-source/company items, logs, and GitHub Actions metadata |
| `reviews` | Permanent failures and manual-review cases |

The extra tables from the first iteration (`daily_runs`, `run_items`, `company_reviews`, `job_events`, `sync_state`, `sync_logs`) and the two frontend views were removed. The frontend should query `companies` and `jobs` directly; RLS already exposes only published companies and their available jobs.

The collector uses the service-role connection string and bypasses RLS. The `runs` and `reviews` tables are private to the service role.

## Setup

### 1. Secrets

The existing repository secrets are reused:

| Secret | Required | Notes |
| --- | --- | --- |
| `SIM_DATABASE_URL` | Yes | Supabase Postgres connection string. The code also accepts `SUPABASE_DB_URL` or `DATABASE_URL`. |
| `TELEGRAM_BOT_TOKEN` | No | Enables the daily Telegram summary. |
| `TELEGRAM_CHAT_ID` | No | Optional. The existing chat ID is used as a fallback. |
| `ALERT_WEBHOOK_URL` | No | Optional Discord/Slack-style webhook for failures. |

No new secret is required. If you want to stop publishing the fallback Telegram chat ID, add `TELEGRAM_CHAT_ID` as a secret and remove the fallback from `collector/config.py`.

### 2. Workflow

`.github/workflows/daily-sync.yml` runs at **02:00 UTC** every day and can be started manually with `workflow_dispatch`.

Manual options:

- `mode`: `daily` or `backfill`
- `max_enrich`: candidates to enrich
- `max_new`: new companies to sync jobs for
- `max_refresh`: existing companies to refresh
- `dry_run`: fetch sources only, no database writes

### 3. Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export SIM_DATABASE_URL="postgresql://..."
python -m collector --mode daily
```

Dry run without a database:

```bash
python -m collector --dry-run
```

Backfill more than the daily quota:

```bash
python -m collector --mode backfill --max-enrich 200 --max-new 200 --max-refresh 0
```

## Daily flow

```
discovery -> enrichment -> jobs -> refresh -> report
```

- **Discovery** upserts candidates; it never creates companies directly.
- **Enrichment** claims a bounded batch, calls StartupDB, detects the ATS, scores the company, and upserts it.
- **Jobs** fetches boards for the newly enriched companies, upserts jobs, and closes jobs missing from the board.
- **Refresh** re-syncs companies selected by the activity tier.
- **Report** writes `runs`, sends Telegram, and exits non-zero only on a fatal run failure.

A failed board fetch never deactivates its jobs. Only a successful fetch with a complete job list can close missing jobs.

## Quality rules

A company is published when its score is at least `MIN_QUALITY_SCORE` (default `30`):

- Name: 5
- Website: 15
- Description: 15
- Founded year: 5
- HQ: 5
- Logo: 5
- Industries: 5
- Funding: 15 + 10
- Verified ATS board: 25

This means an ATS-only company with a verified board can still be published and provide jobs, while a company with no website, no ATS, and no useful metadata is rejected into `reviews`.

Hard rejects are limited to:

- no usable company name
- no website and no ATS hint
- score below the threshold

## Idempotency and "do not lose data"

- `companies` is matched by domain first, then normalized name.
- `jobs` is matched by `source_key = provider|region|token|external_id`.
- `company_data` stores a capped history of raw source snapshots.
- `ats_sources` stores every detected board, not just the primary one.
- `jobs.raw_data` keeps the latest raw payload; `jobs.history` keeps open/update/close events.
- `jobs` are soft-closed (`status = 'unavailable'`, `unavailable_on` set), never deleted.
- Re-running the same day is safe.

## ATS support

| Provider | Job API | Detection |
| --- | --- | --- |
| Greenhouse | `boards-api.greenhouse.io` | URL, careers page, slug guess |
| Lever | `api.lever.co` / `api.eu.lever.co` | URL, careers page, slug guess |
| Ashby | `posting-api.ashbyhq.com` | URL, careers page, slug guess |
| Workable | `apply.workable.com/api/v1/widget` | URL |
| SmartRecruiters | `api.smartrecruiters.com/v1` | URL |

Detection order is: explicit discovery link → careers page/iframe → bounded slug guesses. Every candidate board is probed before it is stored.

## Data sources and attribution

- **StartupDB** — free API, data licensed CC BY 4.0. Attribution is stored in the raw snapshot and documented here. StartupDB is used for enrichment only; its search is a name/domain lookup, not an industry search.
- **Hacker News** — Algolia API.
- **Y Combinator** — public company API.
- **RemoteOK / Remotive / We Work Remotely** — public job feeds. They are discovery sources; jobs are stored from the company's ATS.
- **open-jobs-data** and **open-ats-feed** — community ATS catalogues.
- **Techstars ecosystem feed** — public company metadata.

Respect each source's terms and rate limits. The HTTP client spaces requests per host and retries 429/5xx responses with backoff.

## Operations

- GitHub scheduled workflows are disabled after 60 days of repository inactivity. `.github/workflows/keepalive.yml` makes a small monthly commit to prevent that.
- The collector takes a Postgres advisory lock, so two runs cannot overlap.
- Stuck `processing` candidates are reset to `retry` after two hours.
- `runs.items` shows exactly which source or company failed and why.
- `reviews` is the permanent failure queue. Transient failures stay in `candidates` and retry.
- `runs.github` stores the GitHub Actions run ID, workflow, actor, commit, and run URL.

## Tests

CI runs:

```bash
python -m compileall -q collector
python -m unittest discover -s tests -v
```

The tests cover normalization, ATS URL extraction, all five job adapters, scoring, and HN comment parsing. They do not call the network or the database.

## Notes

- The old root-level modules (`engine.py`, `discovery.py`, `providers.py`, `net.py`, `sync_daily.py`, `ai_researcher.py`) are not used by the new workflow. They can be deleted after you confirm you no longer need the old implementation.
- The migration is additive; existing rows are preserved. `jobs.status` keeps the existing `available`/`unavailable` values for frontend compatibility.
- The public views expose only companies with `is_active = true` and `is_public = true`, and only `available` jobs.