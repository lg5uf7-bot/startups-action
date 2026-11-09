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
    text = (f"🚀 *Startup Daily Sync Complete*\n"
            f"Day: `{stats['run_day']}`\n"
            f"Status: `{stats['status']}`\n"
            f"New Companies: {stats['onboarding']['new_companies']} (Total: {stats['companies_total']})\n"
            f"Active Jobs: {stats['jobs_available']}\n"
            f"Pending Reviews: {stats['pending_review_items']}")
            
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
            'status': out['status'],
            'new_companies': out['onboarding'].get('new_companies'),
            'company_total': out['companies_total'],
            'jobs_available': out['jobs_available'],
            'reviews': out['pending_review_items']
        }, indent=2))
        
        send_telegram(out)

if __name__ == '__main__':
    main()
