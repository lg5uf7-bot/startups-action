from __future__ import annotations

import html
import logging
from datetime import date
from typing import Any

import psycopg
import requests

from .. import db
from ..config import Settings

log = logging.getLogger(__name__)


def _send_telegram(settings: Settings, text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Telegram notification failed: %s", exc)
        return False


def _send_webhook(settings: Settings, payload: dict[str, Any]) -> bool:
    if not settings.alert_webhook_url:
        return False
    try:
        response = requests.post(settings.alert_webhook_url, json=payload, timeout=20)
        response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Webhook notification failed: %s", exc)
        return False


def _top_companies(conn: psycopg.Connection, run_day: date) -> list[dict[str, Any]]:
    return conn.execute(
        """
        select name, jobs_count, ats_type, quality_score
        from public.companies
        where created_at::date = %s
        order by jobs_count desc, quality_score desc
        limit 5
        """,
        (run_day,),
    ).fetchall()


def build_message(
    conn: psycopg.Connection,
    *,
    run_day: date,
    status: str,
    stats: dict[str, Any],
    totals: dict[str, int],
    error: str | None = None,
) -> str:
    lines = [
        f"<b>Startup collector — {run_day.isoformat()}</b>",
        f"Status: <b>{html.escape(status)}</b>",
        "",
        f"New candidates: <b>{stats.get('discovered', 0)}</b>",
        f"Companies enriched: <b>{stats.get('enriched', 0)}</b>",
        f"Companies with jobs: <b>{stats.get('succeeded', 0)}</b>",
        f"Jobs upserted: <b>{stats.get('jobs_synced', 0)}</b>",
        f"Reviews queued: <b>{stats.get('reviews', 0)}</b>",
        "",
        f"DB totals: <b>{totals.get('companies', 0)}</b> companies, "
        f"<b>{totals.get('jobs_active', 0)}</b> active jobs, "
        f"<b>{totals.get('reviews_pending', 0)}</b> pending reviews",
    ]
    top = _top_companies(conn, run_day)
    if top:
        lines.extend(["", "<b>Top new companies</b>"])
        for row in top:
            ats = f" ({html.escape(str(row['ats_type']))})" if row.get("ats_type") else ""
            lines.append(
                f"• {html.escape(str(row['name']))} — "
                f"{int(row.get('jobs_count') or 0)} jobs{ats}"
            )
    if error:
        lines.extend(["", f"<b>Error:</b> {html.escape(error[:800])}"])
    return "\n".join(lines)


def send_report(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    run_day: date,
    status: str,
    stats: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    totals = db.report_totals(conn)
    message = build_message(
        conn,
        run_day=run_day,
        status=status,
        stats=stats,
        totals=totals,
        error=error,
    )
    telegram = _send_telegram(settings, message)
    webhook = _send_webhook(
        settings,
        {
            "event": "startup_collector",
            "status": status,
            "run_day": run_day.isoformat(),
            "stats": stats,
            "totals": totals,
            "error": error,
        },
    )
    return {
        "telegram": telegram,
        "webhook": webhook,
        "totals": totals,
        "message": message,
    }