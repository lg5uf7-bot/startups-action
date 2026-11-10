from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

from . import db
from .ats.detect import detect_ats
from .config import load_settings
from .http import HttpClient
from .pipeline import discovery, enrich, jobs, refresh, report
from .sources import feeds, hn, remoteok, remotive, startupdb, wwr, yc

log = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily startup + jobs collector")
    parser.add_argument("--mode", default="daily", choices=["daily", "backfill"])
    parser.add_argument("--dry-run", action="store_true", help="fetch sources only; no DB writes")
    parser.add_argument("--max-enrich", type=int, default=None)
    parser.add_argument("--max-new", type=int, default=None)
    parser.add_argument("--max-refresh", type=int, default=None)
    parser.add_argument("--run-key", default=None)
    return parser.parse_args(argv)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _dry_run(settings) -> int:
    client = HttpClient(settings)
    run_day = datetime.now(timezone.utc).date()
    print(f"Dry run for {run_day.isoformat()} — no database writes")
    groups = {
        "open_jobs_catalog": feeds.fetch_open_jobs_catalog(client),
        "open_ats_feed": feeds.fetch_ats_feed(client, run_day, limit=20),
        "techstars_feed": feeds.fetch_techstars(client, run_day, limit=10),
        "yc": yc.fetch(client, run_day, limit=10),
        "hn_who_is_hiring": hn.fetch(client, limit=10),
        "remoteok": remoteok.fetch(client, limit=10),
        "remotive": remotive.fetch(client, limit=10),
        "weworkremotely": wwr.fetch(client, limit=10),
    }
    for name, candidates in groups.items():
        print(f"{name}: {len(candidates)} candidates")
        for candidate in candidates[:3]:
            print(
                f"  - {candidate.name} | domain={candidate.domain} | "
                f"ats={candidate.ats_type}/{candidate.ats_slug}"
            )
    for candidate in groups["hn_who_is_hiring"][:3]:
        board = detect_ats(client, candidate)
        print(f"ATS check {candidate.name}: {board.provider}/{board.token}" if board else f"ATS check {candidate.name}: none")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()
    overrides: dict[str, Any] = {}
    if args.max_enrich is not None:
        overrides["max_enrich"] = args.max_enrich
    if args.max_new is not None:
        overrides["max_new_jobs"] = args.max_new
    if args.max_refresh is not None:
        overrides["max_refresh"] = args.max_refresh
    if overrides:
        settings = replace(settings, **overrides)
    _configure_logging(settings.log_level)

    if args.dry_run or settings.dry_run:
        return _dry_run(settings)

    if not settings.database_url:
        log.error("SIM_DATABASE_URL is not set")
        return 2

    conn = db.connect(settings)
    if not db.try_lock(conn):
        log.warning("Another collector run is already active; exiting")
        conn.close()
        return 0

    run_day = datetime.now(timezone.utc).date()
    run_key = args.run_key or f"{run_day.isoformat()}:{args.mode}"
    run = db.start_run(conn, run_key, run_day, args.mode)
    run_id = str(run["id"])
    log.info("run %s started (%s)", run_key, run_id)

    client = HttpClient(settings)
    startupdb_client = startupdb.StartupDB(client, delay=settings.startupdb_delay)
    deadline = time.monotonic() + settings.max_runtime_seconds

    stats: dict[str, Any] = {
        "discovered": 0,
        "enriched": 0,
        "succeeded": 0,
        "failed": 0,
        "jobs_synced": 0,
        "reviews": 0,
    }
    phase_timings: dict[str, Any] = {}
    status = "success"
    error: str | None = None

    try:
        db.reset_stuck_candidates(conn)

        started = time.monotonic()
        discovery_stats = discovery.run_discovery(
            conn, client, settings, run, run_day
        )
        phase_timings["discovery"] = int((time.monotonic() - started) * 1000)
        stats["discovered"] = discovery_stats["new_candidates"]

        started = time.monotonic()
        enrichment_stats = enrich.run_enrichment(
            conn, client, settings, run, run_day, startupdb_client
        )
        phase_timings["enrichment"] = int((time.monotonic() - started) * 1000)
        stats["enriched"] = enrichment_stats["enriched"]
        stats["failed"] += enrichment_stats["failed"]
        stats["reviews"] = enrichment_stats["reviews"]

        started = time.monotonic()
        jobs_stats = jobs.run_jobs(
            conn,
            client,
            settings,
            run,
            run_day,
            enrichment_stats["company_ids"],
        )
        phase_timings["jobs"] = int((time.monotonic() - started) * 1000)
        stats["succeeded"] = jobs_stats["succeeded"]
        stats["failed"] += jobs_stats["failed"]
        stats["jobs_synced"] += jobs_stats["jobs_synced"]

        started = time.monotonic()
        refresh_stats = refresh.run_refresh(
            conn,
            client,
            settings,
            run,
            run_day,
            deadline=deadline,
        )
        phase_timings["refresh"] = int((time.monotonic() - started) * 1000)
        stats["failed"] += refresh_stats["failed"]
        stats["jobs_synced"] += refresh_stats["jobs_synced"]

        if args.mode == "daily" and stats["succeeded"] < 3:
            status = "warning"
            log.warning("only %s new companies have jobs", stats["succeeded"])

        db.finish_run(
            conn,
            run_id,
            status=status,
            discovered=stats["discovered"],
            enriched=stats["enriched"],
            succeeded=stats["succeeded"],
            failed=stats["failed"],
            jobs_synced=stats["jobs_synced"],
            reviews=stats["reviews"],
            phase_timings=phase_timings,
            notes=f"discovery={discovery_stats.get('ingested', 0)} ingested",
        )
    except Exception as exc:
        conn.rollback()
        status = "failed"
        error = str(exc)[:1000]
        log.exception("collector run failed")
        try:
            db.finish_run(
                conn,
                run_id,
                status=status,
                discovered=stats["discovered"],
                enriched=stats["enriched"],
                succeeded=stats["succeeded"],
                failed=stats["failed"],
                jobs_synced=stats["jobs_synced"],
                reviews=stats["reviews"],
                phase_timings=phase_timings,
                error=error,
            )
        except Exception:
            conn.rollback()

    try:
        report.send_report(
            conn,
            settings,
            run_day=run_day,
            status=status,
            stats=stats,
            error=error,
        )
    except Exception:
        log.exception("report failed")

    db.unlock(conn)
    conn.close()
    log.info("run finished: status=%s stats=%s", status, stats)
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())