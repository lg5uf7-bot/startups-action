import os, sys, json, uuid
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
from urllib import request
from psycopg.types.json import Jsonb

from discovery import Catalog, FEED_URL
from engine import run_day
from net import JsonClient

def send_telegram(stats):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        print("TELEGRAM_BOT_TOKEN not found, skipping notification.")
        return
    
    chat_id = "8420407013"
    
    new_companies_count = stats.get('onboarding', {}).get('new_companies', 0)
    new_ids = set(stats.get('onboarding', {}).get('new_company_ids', []))
    new_names = [r['name'] for r in stats.get('refresh', []) if r['company_id'] in new_ids]
    
    changed_companies = [r for r in stats.get('refresh', []) if r.get('new', 0) > 0 or r.get('updated', 0) > 0]
    changed_companies.sort(key=lambda x: (x.get('new',0), x.get('updated',0)), reverse=True)
    
    text = (f"🚀 *Startup Daily Sync Complete*\n"
            f"Day: `{stats['run_day']}` | Status: `{stats.get('status', 'unknown')}`\n\n"
            f"🏢 *Companies*\n"
            f"• Added Today: {new_companies_count}\n"
            f"• Total Active: {stats.get('companies_total', 0)}\n\n"
            f"💼 *Jobs*\n"
            f"• Active: {stats.get('jobs_available', 0)}\n"
            f"• Closed/Unavailable: {stats.get('jobs_unavailable', 0)}\n\n")

    if new_names:
        text += f"🆕 *New Companies Added*\n"
        for name in new_names:
            text += f"• {name}\n"
        text += "\n"

    if changed_companies:
        text += "📈 *Job Updates (Top 10)*\n"
        for r in changed_companies[:10]:
            name = r['name']
            new = r.get('new', 0)
            updated = r.get('updated', 0)
            text += f"• {name}: +{new} added, ~{updated} updated\n"
        if len(changed_companies) > 10:
            text += f"... and {len(changed_companies) - 10} more.\n"
            
    if stats.get('pending_review_items', 0) > 0:
        text += f"\n⚠️ *Pending Reviews*: {stats['pending_review_items']} (Check DB/Logs)\n"

    errors = stats.get('errors', [])
    discovery_info = stats.get('discovery')
    discovery_error = discovery_info.get('error') if isinstance(discovery_info, dict) else None
    
    if errors or discovery_error or stats.get('status') == 'failed':
        text += f"\n❌ *Errors & Issues*\n"
        if discovery_error:
            text += f"• Discovery: {discovery_error}\n"
        for err in errors[:5]:
            text += f"• {err.get('phase', 'Error')}: {err.get('error', '')}\n"
        if len(errors) > 5:
            text += f"• ...and {len(errors) - 5} more.\n"
            
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode('utf-8')
    req = request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        request.urlopen(req)
        print("Telegram notification sent successfully.")
    except Exception as e:
        print("Failed to send Telegram notification:", e)

def main():
    dsn = os.environ.get('SIM_DATABASE_URL')
    if not dsn:
        print("Missing SIM_DATABASE_URL environment variable.")
        sys.exit(1)

    today = datetime.now(timezone.utc).date()
    print(f"Starting daily sync for {today}...")

    client = JsonClient(delay=1.05)
    
    # In production, fetch the open-source candidate catalogs exactly once per run
    named = client.get('https://raw.githubusercontent.com/veresk06/open-ats-feed/main/actor/data/companies.json', 'catalog_named')
    tokens = client.get('https://raw.githubusercontent.com/ConorsCode/open-jobs-data/main/companies.json', 'catalog_tokens')
    catalog = Catalog(named, tokens)

    def load_feed():
        # Fetch actual live JSON
        return client.get(FEED_URL, 'company_feed')

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        out = run_day(conn, today, load_feed, catalog, client)
        conn.execute('UPDATE public.sync_logs SET details=%s WHERE id=%s', (Jsonb(out), out['run_id']))
        print("Sync completed. Summary:")
        print(json.dumps({
            'status': out.get('status'),
            'new_companies': out.get('onboarding', {}).get('new_companies'),
            'company_total': out.get('companies_total'),
            'jobs_available': out.get('jobs_available'),
            'reviews': out.get('pending_review_items')
        }, indent=2))
        
        send_telegram(out)

if __name__ == '__main__':
    main()
