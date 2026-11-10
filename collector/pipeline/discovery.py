from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Callable

import psycopg

from .. import db
from ..config import Settings
from ..http import HttpClient
from ..models import Candidate
from ..sources import feeds, hn, remoteok, remotive, wwr, yc

log = logging.getLogger(__name__)


def _ingest(
    conn: psycopg.Connection,
    source_name: str,
    candidates: list[Candidate],
) -> int:
    for index, candidate in enumerate(candidates, start=1):
        db.upsert_candidate(conn, candidate)
        if index % 50 == 0:
            conn.commit()
    conn.commit()
    return len(candidates)


def run_discovery(
    conn: psycopg.Connection,
    client: HttpClient,
    settings: Settings,
    run: dict[str, Any],
    run_day: date,
) -> dict[str, Any]:
    run_id = str(run["id"])
    sources: list[tuple[str, Callable[[], list[Candidate]]]] = [
        ("open_jobs_catalog", lambda: feeds.fetch_open_jobs_catalog(client)),
        ("open_ats_feed", lambda: feeds.fetch_ats_feed(client, run_day, limit=200)),
        ("techstars_feed", lambda: feeds.fetch_techstars(client, run_day, limit=120)),
        ("yc", lambda: yc.fetch(client, run_day, limit=100)),
        ("hn_who_is_hiring", lambda: hn.fetch(client, limit=120)),
        ("remoteok", lambda: remoteok.fetch(client, limit=100)),
        ("remotive", lambda: remotive.fetch(client, limit=80)),
        ("weworkremotely", lambda: wwr.fetch(client, limit=80)),
    ]

    total = 0
    source_stats: dict[str, Any] = {}
    for source_name, loader in sources:
        started = time.monotonic()
        try:
            candidates = loader()
            count = _ingest(conn, source_name, candidates)
            total += count
            duration = int((time.monotonic() - started) * 1000)
            source_stats[source_name] = {"count": count, "duration_ms": duration}
            db.record_item(
                conn,
                run_id,
                "discovery",
                source_name,
                "ok",
                duration_ms=duration,
                details={"count": count},
            )
            conn.commit()
            log.info("discovery %s: %s candidates", source_name, count)
        except Exception as exc:  # one bad source must not stop the others
            conn.rollback()
            duration = int((time.monotonic() - started) * 1000)
            source_stats[source_name] = {"count": 0, "error": str(exc)[:300]}
            db.record_item(
                conn,
                run_id,
                "discovery",
                source_name,
                "failed",
                error_code="source_error",
                duration_ms=duration,
                details={"error": str(exc)[:300]},
            )
            conn.commit()
            log.exception("discovery source %s failed", source_name)

    row = conn.execute(
        "select count(*) as n from public.candidates where discovered_at::date = %s",
        (run_day,),
    ).fetchone()
    new_candidates = int(row["n"] or 0) if row else 0

    db.update_run_metadata(
        conn,
        run_id,
        {
            "discovery_last_run": {
                "run_day": run_day.isoformat(),
                "ingested": total,
                "new_candidates": new_candidates,
                "sources": source_stats,
            }
        },
    )
    return {
        "ingested": total,
        "new_candidates": new_candidates,
        "sources": source_stats,
    }