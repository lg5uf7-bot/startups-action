"""Deterministic candidate discovery and conservative ownership evidence."""
from __future__ import annotations
import hashlib, json, re, unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, parse_qs, unquote, urlencode, quote
import tldextract

EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
# Never treat a shared social/profile domain as the employer's own homepage.
BLOCKED = {'linkedin.com','facebook.com','twitter.com','x.com','github.com','crunchbase.com','instagram.com',
           'youtube.com','linktr.ee','medium.com','notion.so','google.com','wellfound.com','wikipedia.org',
           'greenhouse.io','ashbyhq.com','lever.co','gem.com','smartrecruiters.com'}
CATALOG_NAMED = 'https://raw.githubusercontent.com/ConorsCode/open-jobs-data/main/companies.json'
CATALOG_TOKENS = 'https://raw.githubusercontent.com/veresk06/open-ats-feed/main/actor/data/companies.json'
FEED_URL = 'https://raw.githubusercontent.com/yigitmeteozcan/startups/refs/heads/main/data/all.json'

def name_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value or '')).casefold() if c.isalnum())

def valid_http_url(url):
    try:
        p = urlsplit(str(url or '').strip())
        return p.scheme in ('http','https') and bool(p.hostname) and p.username is None and p.password is None
    except ValueError:
        return False

def safe_domain(url):
    if not valid_http_url(url): return None
    host = urlsplit(url).hostname.lower().rstrip('.')
    p = EXTRACT(host)
    if not p.suffix or not p.domain: return None
    domain = (p.domain + '.' + p.suffix).lower()
    return None if domain in BLOCKED else domain

def normalized_url(url):
    if not valid_http_url(url): return ''
    p = urlsplit(url.strip())
    # Tracking parameters only. Keep job identifiers and other meaningful query parameters.
    q = [(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() != 'gh_src']
    return urlunsplit((p.scheme.lower(), (p.hostname or '').lower() + ((':'+str(p.port)) if p.port and p.port not in (80,443) else ''), p.path.rstrip('/') or '/', urlencode(sorted(q)), ''))

def url_has_job_id(url, ident):
    if not valid_http_url(url) or not ident: return False
    parsed=urlsplit(url)
    path_parts=[unquote(x) for x in parsed.path.split('/') if x]
    query_values=[v for values in parse_qs(parsed.query).values() for v in values]
    return str(ident) in path_parts or str(ident) in query_values

def source_key(record):
    # all.json does not expose a universal stable ID. Keep source/cohort identity and flag ambiguity.
    identity = {'source':record.get('source'), 'name':name_key(record.get('name')),
                'domain':safe_domain(record.get('website')), 'year':record.get('year'),
                'program':record.get('program'), 'programSlugs':record.get('programSlugs')}
    return hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def board_urls(provider, token, region='global'):
    t=quote(str(token),safe='-_')
    if provider=='greenhouse': return f'https://job-boards.greenhouse.io/{t}', f'https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true'
    if provider=='ashby': return f'https://jobs.ashbyhq.com/{t}', f'https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true'
    if provider=='lever':
        host='eu.' if region=='eu' else ''
        return f'https://jobs.{host}lever.co/{t}', f'https://api.{host}lever.co/v0/postings/{t}?mode=json&limit=100&skip=0'
    if provider=='gem': return f'https://jobs.gem.com/{t}', f'https://api.gem.com/job_board/v0/{t}/job_posts/'
    if provider=='smartrecruiters': return f'https://jobs.smartrecruiters.com/{t}', f'https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100&offset=0'
    return None, None

class Catalog:
    def __init__(self, named, token_data):
        self.index = {}
        if isinstance(named,list):
            for row in named:
                if not isinstance(row,dict): continue
                provider=str(row.get('platform') or '').lower(); token=str(row.get('slug') or '')
                if not row.get('name') or not token: continue
                self._add(name_key(row['name']), provider, token, CATALOG_NAMED, 'exact catalog name')
        if isinstance(token_data,dict):
            for provider, groups in (token_data.get('providers') or {}).items():
                if not isinstance(groups,dict): continue
                for item in (groups.get('live') or []):
                    token = item[0] if isinstance(item,list) and item else None
                    if isinstance(token,str) and token:
                        self._add(name_key(token),provider.lower(),token,CATALOG_TOKENS,'exact token/name or token/domain')
    def _add(self, key, provider, token, source, reason):
        if key and len(key)>=3 and re.fullmatch(r'[A-Za-z0-9_\-\.]+',token):
            self.index.setdefault(key,[]).append((provider,token,source,reason))
    def candidates(self, company):
        keys={name_key(company.get('name')): 'company name'}
        domain=safe_domain(company.get('website'))
        if domain:
            keys[name_key(EXTRACT(domain).domain)]='website domain label'
        # Optional structured company-profile handles are candidate hints only.
        for wrapped in company.get('source_records') or []:
            rec=wrapped.get('record',{}) if isinstance(wrapped,dict) else {}
            social=rec.get('social') or {}
            if not isinstance(social,dict): continue
            value=social.get('linkedin') or ''
            if valid_http_url(value):
                p=urlsplit(value); parts=p.path.strip('/').split('/')
                host=(p.hostname or '').lower()
                if (host=='linkedin.com' or host.endswith('.linkedin.com')) and len(parts)>=2 and parts[0]=='company' and not parts[1].isdigit():
                    keys[name_key(parts[1])]='feed LinkedIn company handle'
        grouped={}
        def add(key, why, suffixed=False):
            for provider,token,src,reason in self.index.get(key,[]):
                if suffixed and src!=CATALOG_TOKENS: continue
                board_url, api_url = board_urls(provider,token)
                if not board_url: continue
                ident=(provider, token)
                obj=grouped.setdefault(ident, {'provider':provider,'token':token,'region':'global','board_url':board_url,'api_url':api_url,
                                                'status':'candidate','evidence':{'catalogs':[],'matching_reasons':[]}})
                if src not in obj['evidence']['catalogs']: obj['evidence']['catalogs'].append(src)
                new_reason=why + (' + bounded common/numeric suffix' if suffixed else ' + exact catalog match')
                if new_reason not in obj['evidence']['matching_reasons']: obj['evidence']['matching_reasons'].append(new_reason)
        for key,why in sorted(keys.items()):
            if len(key)<3: continue
            add(key,why)
            # Limited token variants improve discovery, but never approve an employer.
            if len(key)>=7:
                for suffix in ('inc','hq','jobs','careers','global','us')+tuple(str(x) for x in range(100)):
                    add(key+suffix,why,True)
        all_candidates=sorted(grouped.values(), key=lambda b:(b['provider'],b['token']))
        # Bounded input: if more than eight candidates, report truncation; do not pretend discovery is exhaustive.
        return all_candidates[:8], len(all_candidates)>8

def verify_company_board(company, board, metadata, jobs):
    """Evidence gate, not just name/HTTP 200: independent source site + exact board name +
    two distinct provider-issued *job URLs on that same employer domain*. No page is fetched.
    Direct, first-party API-sourced board assertions can be added later, with explicit job_sources.
    """
    domain=safe_domain(company.get('website'))
    ev=board.setdefault('evidence',{})
    ev.update({'feed_name':company.get('name'), 'feed_website':company.get('website'), 'feed_domain':domain,
               'board_name':metadata.get('name') if isinstance(metadata,dict) else None})
    matches=[]
    for j in jobs:
        u=j.get('job_url'); ident=str(j.get('external_id') or '')
        if not domain or not ident or safe_domain(u)!=domain or not normalized_url(u):
            continue
        # A generic corporate homepage reused for every posting is NOT enough. The
        # employer-domain URL must actually contain the provider's individual job ID.
        if url_has_job_id(u,ident):
            matches.append({'external_id':ident,'url':u})
    ev['matching_company_domain_job_urls']=matches[:5]
    ev['matching_unique_job_count']=len({x['external_id'] for x in matches})
    exact_name=bool(name_key(company.get('name')) and name_key(company.get('name'))==name_key(ev['board_name']))
    if (board['provider']=='greenhouse' and domain and exact_name and ev['matching_unique_job_count']>=2
        and ev.get('catalogs')):
        ev['decision']='verified_by_feed_domain_plus_provider_custom_job_urls_and_exact_board_name'
        ev['note']='Cross-source corroboration, not a guarantee; source-site accuracy and rights still matter.'
        return True
    ev['decision']='needs_review'
    ev['note']='Candidate only: no sufficient cross-source employer-domain proof.'
    return False
