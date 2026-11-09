import os
import sys
import json
import urllib.request
import urllib.parse
import psycopg
from psycopg.rows import dict_row

JINA_API_URL = "https://r.jina.ai/"
DUCKDUCKGO_LITE_URL = "https://html.duckduckgo.com/html/"
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DB_URL = os.environ.get('SIM_DATABASE_URL')

MODELS = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]

def search_web(query):
    print(f"    [Tool] Searching web for: {query}")
    data = urllib.parse.urlencode({'q': query}).encode('utf-8')
    req = urllib.request.Request(DUCKDUCKGO_LITE_URL, data=data, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req) as response:
            html = response.read().decode('utf-8')
            urls = []
            import re
            for match in re.finditer(r'href="(https?://[^"]+)"', html):
                url = match.group(1)
                if 'duckduckgo.com' not in url and url not in urls: urls.append(url)
            return f"Found URLs: {', '.join(urls[:5])}"
    except Exception as e:
        return f"Search failed: {e}"

def read_page(url):
    print(f"    [Tool] Reading page: {url}")
    req = urllib.request.Request(JINA_API_URL + url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8')[:4000]
    except Exception as e:
        return f"Failed to read page: {e}"

def call_gemini(system_prompt, messages, model_name):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": messages,
        "tools": [{"functionDeclarations": [
            {"name": "search_web", "description": "Search web for job boards.", "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}, "required": ["query"]}},
            {"name": "read_page", "description": "Read webpage Markdown.", "parameters": {"type": "OBJECT", "properties": {"url": {"type": "STRING"}}, "required": ["url"]}}
        ]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode('utf-8'))

def investigate_company(name, website):
    system_prompt = f"""You are an elite ATS Investigator. Find the Applicant Tracking System (ATS) API token for the company "{name}" (Website: "{website}").
You have two tools:
1. `search_web`: Search Google/DuckDuckGo for "{name} greenhouse lever ashby workable smartrecruiters".
2. `read_page`: Read the Markdown of their career page to find ATS links.
Your final output MUST be a strict JSON object:
{{"provider": "greenhouse/lever/ashby/workable/smartrecruiters", "token": "the_extracted_token", "evidence": "Your 2-sentence proof."}}
If absolutely not found, return: {{"provider": null, "token": null, "evidence": "Could not be found."}}"""
    
    messages = [{"role": "user", "parts": [{"text": f"Find the ATS token for {name}."}]}]
    
    for model in MODELS:
        print(f"  Attempting with model: {model}...")
        try:
            for _ in range(4):
                response = call_gemini(system_prompt, messages, model)
                part = response['candidates'][0]['content']['parts'][0]
                if 'functionCall' in part:
                    func_name = part['functionCall']['name']
                    args = part['functionCall'].get('args', {})
                    messages.append({"role": "model", "parts": [part]})
                    if func_name == "search_web": result = search_web(args.get('query'))
                    elif func_name == "read_page": result = read_page(args.get('url'))
                    else: result = "Unknown function"
                    messages.append({"role": "function", "parts": [{"functionResponse": {"name": func_name, "response": {"result": result}}}]})
                else:
                    return json.loads(part['text'])
        except Exception as e:
            print(f"  [Error] {model} failed: {e}")
            continue
    return {"provider": None, "token": None, "evidence": "All models failed or timed out."}

def main():
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY not found. Skipping AI review.")
        sys.exit(0)
    if not DB_URL:
        print("SIM_DATABASE_URL not found.")
        sys.exit(1)

    with psycopg.connect(DB_URL, autocommit=True, row_factory=dict_row) as conn:
        # UPDATED: Now checks for BOTH 'no_ats_found' and 'source_candidate'
        pending = conn.execute("SELECT * FROM public.sync_logs WHERE kind='review' AND phase IN ('no_ats_found', 'source_candidate') AND status='pending'").fetchall()
        if not pending:
            print("No pending AI reviews.")
            return

        for review in pending:
            company = conn.execute("SELECT * FROM public.companies WHERE id=%s", (review['company_id'],)).fetchone()
            print(f"\n🔍 Investigating: {company['name']} ({company['website']})")
            
            result = investigate_company(company['name'], company['website'])
            print(f"  Result: {result}")
            
            old_details = review['details']
            old_details['ai_investigation'] = result
            
            conn.execute(
                "UPDATE public.sync_logs SET details=%s, updated_at=now() WHERE id=%s",
                (json.dumps(old_details), review['id'])
            )
            
            token = os.environ.get('TELEGRAM_BOT_TOKEN')
            if token:
                text = (f"🤖 *AI Investigator Report*\n"
                        f"**Target:** {company['name']}\n"
                        f"**Provider:** `{result.get('provider', 'None')}`\n"
                        f"**Token:** `{result.get('token', 'None')}`\n"
                        f"**Evidence:** {result.get('evidence')}\n\n"
                        f"_(Check Supabase to verify!)_")
                try:
                    req = urllib.request.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=json.dumps({"chat_id": "8420407013", "text": text, "parse_mode": "Markdown"}).encode('utf-8'),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(req)
                except Exception as e:
                    print(f"Failed to send Telegram alert: {e}")

if __name__ == "__main__":
    main()
