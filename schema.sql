-- Startup collector schema: five tables only.
--
-- Final tables:
--   companies  - published company profiles
--   jobs       - active and closed jobs
--   candidates - discovery queue
--   runs       - daily run + GitHub Actions information
--   reviews    - permanent/manual-review queue
--
-- This migration is additive for companies/jobs. It creates runs and reviews,
-- copies the old sync_logs rows into them, then removes the extra tables and
-- views from the earlier iteration. It is safe to run more than once.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- companies
-- ---------------------------------------------------------------------------
alter table public.companies add column if not exists slug text;
alter table public.companies add column if not exists short_description text;
alter table public.companies add column if not exists founded_year integer;
alter table public.companies add column if not exists headquarters text;
alter table public.companies add column if not exists country text;
alter table public.companies add column if not exists city text;
alter table public.companies add column if not exists linkedin_url text;
alter table public.companies add column if not exists x_url text;
alter table public.companies add column if not exists logo_url text;
alter table public.companies add column if not exists careers_url text;
alter table public.companies add column if not exists employee_count integer;
alter table public.companies add column if not exists industries text[];
alter table public.companies add column if not exists tags text[];
alter table public.companies add column if not exists last_funding_round text;
alter table public.companies add column if not exists last_funding_date date;
alter table public.companies add column if not exists funding_date_raw text;
alter table public.companies add column if not exists funding_date_precision text;
alter table public.companies add column if not exists last_funding_amount numeric;
alter table public.companies add column if not exists last_funding_currency text;
alter table public.companies add column if not exists total_funding_usd numeric;
alter table public.companies add column if not exists ats_type text;
alter table public.companies add column if not exists ats_slug text;
alter table public.companies add column if not exists ats_board_url text;
alter table public.companies add column if not exists ats_detected_via text;
alter table public.companies add column if not exists ats_confidence numeric;
alter table public.companies add column if not exists quality_score integer not null default 0;
alter table public.companies add column if not exists jobs_count integer not null default 0;
alter table public.companies add column if not exists jobs_status text not null default 'unknown';
alter table public.companies add column if not exists is_active boolean not null default true;
alter table public.companies add column if not exists is_public boolean not null default true;
alter table public.companies add column if not exists last_checked_at timestamptz;
alter table public.companies add column if not exists last_jobs_synced_at timestamptz;
alter table public.companies add column if not exists last_changed_at timestamptz;
alter table public.companies add column if not exists content_hash text;
alter table public.companies add column if not exists enrichment_status text not null default 'none';
alter table public.companies add column if not exists source_synced_at timestamptz;
alter table public.companies add column if not exists discovery_source text;
alter table public.companies add column if not exists discovery_url text;

create unique index if not exists companies_domain_uidx
    on public.companies (lower(domain))
    where domain is not null and domain <> '';
create unique index if not exists companies_slug_uidx
    on public.companies (slug)
    where slug is not null;
create index if not exists companies_ats_idx
    on public.companies (ats_type, ats_slug);
create index if not exists companies_jobs_status_idx
    on public.companies (jobs_status, jobs_count desc);
create index if not exists companies_last_checked_idx
    on public.companies (last_checked_at nulls first);
create index if not exists companies_quality_idx
    on public.companies (quality_score desc, created_at desc);
create index if not exists companies_domain_trgm_idx
    on public.companies using gin (domain gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- jobs
-- ---------------------------------------------------------------------------
alter table public.jobs add column if not exists description text;
alter table public.jobs add column if not exists description_text text;
alter table public.jobs add column if not exists department text;
alter table public.jobs add column if not exists is_remote boolean;
alter table public.jobs add column if not exists apply_url text;
alter table public.jobs add column if not exists published_at timestamptz;
alter table public.jobs add column if not exists last_seen_at timestamptz;
alter table public.jobs add column if not exists closed_at timestamptz;
alter table public.jobs add column if not exists salary_min numeric;
alter table public.jobs add column if not exists salary_max numeric;
alter table public.jobs add column if not exists salary_currency text;
alter table public.jobs add column if not exists search_vector tsvector
    generated always as (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(description_text, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(location, '')), 'C')
    ) stored;

create index if not exists jobs_status_published_idx
    on public.jobs (status, published_at desc nulls last);
create index if not exists jobs_provider_external_idx
    on public.jobs (provider, external_id);
create index if not exists jobs_search_idx
    on public.jobs using gin (search_vector);
create index if not exists jobs_remote_idx
    on public.jobs (is_remote) where is_remote is true;
create index if not exists jobs_last_seen_idx
    on public.jobs (last_seen_at desc nulls last);

-- ---------------------------------------------------------------------------
-- candidates: discovery queue only
-- ---------------------------------------------------------------------------
create table if not exists public.candidates (
    id uuid primary key default gen_random_uuid(),
    company_key text not null unique,
    name text,
    domain text,
    website_url text,
    ats_type text,
    ats_slug text,
    discovery_source text,
    discovery_url text,
    discovery_sources text[] not null default '{}',
    status text not null default 'new',
    attempts integer not null default 0,
    last_error text,
    raw_data jsonb not null default '{}'::jsonb,
    discovered_at timestamptz not null default now(),
    processed_at timestamptz,
    updated_at timestamptz not null default now(),
    constraint candidates_status_check check (
        status in ('new', 'processing', 'retry', 'enriched', 'synced', 'duplicate', 'rejected')
    )
);
create index if not exists candidates_status_idx
    on public.candidates (status, discovered_at);
create index if not exists candidates_domain_idx
    on public.candidates (lower(domain));
create index if not exists candidates_ats_idx
    on public.candidates (ats_type, ats_slug);

-- ---------------------------------------------------------------------------
-- runs: one row per collector run, including GitHub Actions metadata
-- ---------------------------------------------------------------------------
create table if not exists public.runs (
    id uuid primary key default gen_random_uuid(),
    run_key text not null unique,
    run_date date not null,
    mode text not null default 'daily',
    status text not null default 'running',
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    discovered integer not null default 0,
    enriched integer not null default 0,
    succeeded integer not null default 0,
    failed integer not null default 0,
    jobs_synced integer not null default 0,
    reviews integer not null default 0,
    phase_timings jsonb not null default '{}'::jsonb,
    items jsonb not null default '[]'::jsonb,
    logs jsonb not null default '[]'::jsonb,
    github jsonb not null default '{}'::jsonb,
    metadata jsonb not null default '{}'::jsonb,
    error text,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists runs_date_idx
    on public.runs (run_date desc);
create index if not exists runs_status_idx
    on public.runs (status, started_at desc);

-- ---------------------------------------------------------------------------
-- reviews: permanent failures and manual-review cases
-- ---------------------------------------------------------------------------
create table if not exists public.reviews (
    id uuid primary key default gen_random_uuid(),
    run_id uuid references public.runs(id) on delete set null,
    run_day date,
    company_id uuid references public.companies(id) on delete set null,
    name text not null,
    domain text,
    slug text,
    website text,
    reason_code text not null,
    details text not null,
    raw_json jsonb not null default '{}'::jsonb,
    attempts integer not null default 1,
    next_retry_at timestamptz,
    review_status text not null default 'pending',
    dedupe_key text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint reviews_status_check check (
        review_status in ('pending', 'resolved', 'ignored')
    )
);
create index if not exists reviews_status_idx
    on public.reviews (review_status, created_at);
create index if not exists reviews_domain_idx
    on public.reviews (lower(domain));

-- ---------------------------------------------------------------------------
-- migrate the old sync_logs rows before removing the old tables
-- ---------------------------------------------------------------------------
do $$
begin
    if to_regclass('public.sync_logs') is not null then
        with legacy as (
            select s.*, coalesce(s.details, '{}'::jsonb) as d
            from public.sync_logs s
            where s.kind = 'run'
        )
        insert into public.runs (
            id, run_key, run_date, mode, status, started_at, finished_at,
            discovered, enriched, succeeded, failed, jobs_synced, reviews,
            phase_timings, items, logs, github, metadata, error, notes,
            created_at, updated_at
        )
        select
            legacy.id,
            'legacy:' || legacy.id::text,
            legacy.run_day,
            'legacy',
            legacy.status,
            legacy.created_at,
            legacy.updated_at,
            coalesce((legacy.d->'onboarding'->>'new_companies')::int, 0),
            coalesce((legacy.d->'onboarding'->>'new_companies')::int, 0),
            coalesce((legacy.d->'onboarding'->>'new_companies')::int, 0),
            coalesce(jsonb_array_length(coalesce(legacy.d->'errors', '[]'::jsonb)), 0),
            0,
            coalesce((legacy.d->>'pending_review_items')::int, 0),
            '{}'::jsonb,
            coalesce(legacy.d->'refresh', '[]'::jsonb),
            coalesce(legacy.d->'request_events', '[]'::jsonb),
            '{}'::jsonb,
            legacy.d,
            null,
            'legacy sync_logs import',
            legacy.created_at,
            legacy.updated_at
        from legacy
        on conflict do nothing;

        with legacy as (
            select s.*, coalesce(s.details, '{}'::jsonb) as d
            from public.sync_logs s
            where s.kind = 'review'
        )
        insert into public.reviews (
            id, run_id, run_day, company_id, name, domain, slug, website,
            reason_code, details, raw_json, attempts, review_status,
            dedupe_key, created_at, updated_at
        )
        select
            legacy.id,
            case
                when exists (select 1 from public.runs r where r.id = legacy.run_id)
                    then legacy.run_id
                else null
            end,
            legacy.run_day,
            legacy.company_id,
            coalesce(c.name, legacy.d->'board'->'evidence'->>'feed_name', 'Unknown'),
            c.domain,
            c.slug,
            c.website,
            coalesce(legacy.phase, 'legacy_review'),
            coalesce(legacy.d->>'message', legacy.d::text, ''),
            legacy.d,
            1,
            case
                when legacy.status in ('pending', 'resolved', 'ignored') then legacy.status
                else 'pending'
            end,
            'legacy:' || legacy.id::text,
            legacy.created_at,
            legacy.updated_at
        from legacy
        left join public.companies c on c.id = legacy.company_id
        on conflict do nothing;
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- remove the extra objects from the earlier schema
-- ---------------------------------------------------------------------------
drop view if exists public.public_jobs;
drop view if exists public.public_companies;
drop table if exists public.run_items;
drop table if exists public.daily_runs;
drop table if exists public.job_events;
drop table if exists public.company_reviews;
drop table if exists public.sync_state;
drop table if exists public.sync_logs;

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists companies_set_updated_at on public.companies;
create trigger companies_set_updated_at
    before update on public.companies
    for each row execute function public.set_updated_at();

drop trigger if exists jobs_set_updated_at on public.jobs;
create trigger jobs_set_updated_at
    before update on public.jobs
    for each row execute function public.set_updated_at();

drop trigger if exists candidates_set_updated_at on public.candidates;
create trigger candidates_set_updated_at
    before update on public.candidates
    for each row execute function public.set_updated_at();

drop trigger if exists runs_set_updated_at on public.runs;
create trigger runs_set_updated_at
    before update on public.runs
    for each row execute function public.set_updated_at();

drop trigger if exists reviews_set_updated_at on public.reviews;
create trigger reviews_set_updated_at
    before update on public.reviews
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- RLS: the service role bypasses RLS for the collector; the browser gets only
-- published companies and their currently available jobs.
-- ---------------------------------------------------------------------------
alter table public.companies enable row level security;
alter table public.jobs enable row level security;
alter table public.candidates enable row level security;
alter table public.runs enable row level security;
alter table public.reviews enable row level security;

drop policy if exists "public read companies" on public.companies;
create policy "public read companies"
    on public.companies for select
    to anon, authenticated
    using (is_active and is_public);

drop policy if exists "public read jobs" on public.jobs;
create policy "public read jobs"
    on public.jobs for select
    to anon, authenticated
    using (
        status = 'available'
        and exists (
            select 1
            from public.companies c
            where c.id = jobs.company_id
              and c.is_active
              and c.is_public
        )
    );

revoke all on public.companies, public.jobs from anon, authenticated;
revoke all on public.candidates, public.runs, public.reviews from anon, authenticated;
grant usage on schema public to anon, authenticated;
grant select on public.companies, public.jobs to anon, authenticated;