"""Daily pipeline: deterministic selection, evidence-gated discovery, safe history-preserving refresh.

Uses a PostgreSQL session advisory lock: all writers MUST use this runner for the daily cap
and deduplication guarantees. No API keys or user secrets are included in this file.
"""
from __future__ import annotations
import hashlib, json, traceback, uuid
from datetime import date
from collections import defaultdict
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row
from discovery import name_key, safe_domain, source_key, normalized_url, verify_company_board, url_has_job_id
from providers import fetch_board, get_metadata, IncompleteBoard
from net import FetchError

LOCK_KEY = 461933052617
DAILY_LIMIT = 4

def json_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str,separators=(',',':')).encode()).hexdigest()

def review(conn, run_id, run_day, company_id, phase, key, details):
    row=conn.execute('''INSERT INTO public.sync_logs(run_id,run_day,kind,phase,status,company_id,details)
       VALUES (%s,%s,'review',%s,'pending',%s,%s)
       RETURNING id''',(run_id,run_day,phase,company_id,Jsonb(details))).fetchone()
    return row['id']

def save_company(conn, c):
    conn.execute('''UPDATE public.companies SET company_data=%s, ats_sources=%s, ats_status=%s,
                 updated_at=now() WHERE id=%s''',
                 (Jsonb(c['company_data']),Jsonb(c['ats_sources']),c['ats_status'],c['id']))

def onboard(conn, feed, run_id, day):
    if not isinstance(feed,list) or not feed:
        raise ValueError('Company feed is empty or is not a JSON array')
    if not all(isinstance(r,dict) for r in feed): raise ValueError('Company feed contains a non-object record')
    # One transaction makes an interrupted selection all-or-nothing.
    with conn.transaction():
        existing=conn.execute('SELECT * FROM public.companies').fetchall()
        by_id={str(c['id']):c for c in existing}
        by_name=defaultdict(set); by_domain=defaultdict(set); by_source={}
        for c in existing:
            cid=str(c['id']); by_name[c['normalized_name']].add(cid)
            if c['domain']: by_domain[c['domain']].add(cid)
            for item in c['company_data']:
                if isinstance(item,dict) and item.get('key'):
                    if item['key'] in by_source and by_source[item['key']]!=cid:
                        raise ValueError('Source key attached to multiple companies')
                    by_source[item['key']]=cid
        used=conn.execute("SELECT count(*) AS n FROM public.companies WHERE onboarded_on=%s",(day,)).fetchone()['n']
        created=[]; updated=set(); ambiguous=0; invalid=0; same_records=0
        for rec in feed:
            n=str(rec.get('name') or '').strip(); nk=name_key(n); dom=safe_domain(rec.get('website'))
            if not nk: invalid+=1; continue
            sk=source_key(rec); cid=by_source.get(sk)
            if not cid:
                same_name=by_name.get(nk,set()); same_dom=by_domain.get(dom,set()) if dom else set()
                exact=same_name & same_dom
                if len(exact)==1:
                    cid=next(iter(exact))
                elif same_name or same_dom:
                    # Ambiguity is skipped, not merged or duplicated. Limit noise after the daily selection.
                    ambiguous+=1
                    if used<DAILY_LIMIT:
                        peer=next(iter(sorted(same_name|same_dom)))
                        review(conn,run_id,day,peer,'company_identity',f'company_identity:{peer}:{nk}:{dom or "no-domain"}',
                               {'message':'Possible company collision; not automatically merged or inserted.', 'feed_name':n,'feed_website':rec.get('website'),'source':rec.get('source')})
                    continue
                elif used<DAILY_LIMIT:
                    row=conn.execute('''INSERT INTO public.companies(name,normalized_name,website,domain,onboarded_on)
                         VALUES (%s,%s,%s,%s,%s) RETURNING *''',(n,nk,rec.get('website'),dom,day)).fetchone()
                    cid=str(row['id']); by_id[cid]=row; by_name[nk].add(cid)
                    if dom: by_domain[dom].add(cid)
                    used+=1; created.append(cid)
                else:
                    continue
            c=by_id[cid]; content_hash=json_hash(rec)
            if any(x.get('key')==sk and x.get('hash')==content_hash for x in c['company_data'] if isinstance(x,dict)):
                same_records+=1
            else:
                # Keep distinct raw versions/cohort records; never overwrite manual data.
                c['company_data'].append({'key':sk,'hash':content_hash,'record':rec})
                updated.add(cid)
            by_source[sk]=cid
        for cid in updated:
            save_company(conn,by_id[cid])
        return {'feed_records':len(feed),'new_company_ids':created,'new_companies':len(created),
                'daily_total_after':used,'matched_existing_records':same_records,
                'invalid_feed_records':invalid,'ambiguous_skipped':ambiguous}

def discover_pending(conn, catalog, client, run_id, day):
    companies=conn.execute("SELECT * FROM public.companies WHERE ats_status='pending' ORDER BY created_at,id").fetchall()
    stats={'companies_checked':0,'candidate_boards':0,'verified_boards':0,'candidate_live_nonempty':0,'discovery_errors':0}
    for c in companies:
        stats['companies_checked']+=1
        boards,truncated=catalog.candidates(c)
        if not boards:
            c['ats_status']='no_ats_found'; c['ats_sources']=[]
            with conn.transaction():
                save_company(conn,c)
                review(conn,run_id,day,c['id'],'no_ats_found',f"no_ats_found:{c['id']}",
                       {'message':'No candidate board found in the current catalogs. This does not prove there are no jobs or API.'})
            continue
        stats['candidate_boards']+=len(boards)
        successful_probes=[]
        for b in boards:
            metadata={}
            try:
                metadata=get_metadata(client,b)
                result=fetch_board(client,b)
                b['probe']={'complete':result.complete,'count':len(result.jobs) if result.complete else None,'pages':result.pages,'error':result.error}
                if result.complete and result.jobs:
                    stats['candidate_live_nonempty']+=1
                    successful_probes.append((b,result.jobs))
                b['evidence']['metadata_url']=('https://boards-api.greenhouse.io/v1/boards/'+b['token']) if b['provider']=='greenhouse' else None
                if result.complete and verify_company_board(c,b,metadata,result.jobs):
                    # A board cannot be assigned to two different companies.
                    claimed=False
                    for other in conn.execute('SELECT id,ats_sources FROM public.companies WHERE id<>%s',(c['id'],)).fetchall():
                        if any(x.get('status')=='verified' and x.get('provider')==b['provider'] and x.get('token')==b['token'] and x.get('region','global')==b.get('region','global') for x in other['ats_sources']):
                            claimed=True; break
                    if claimed:
                        b['status']='candidate'; b['evidence']['note']='Board already verified for a different company; review collision.'
                    else:
                        b['status']='verified'; stats['verified_boards']+=1
                else:
                    b['status']='candidate'
            except (FetchError,IncompleteBoard,ValueError,TypeError) as exc:
                b['status']='candidate'; b['probe']={'complete':False,'count':None,'pages':0,'error':str(exc)[:250]}
                b['evidence']['decision']='needs_review'; stats['discovery_errors']+=1
        c['ats_sources']=boards
        c['ats_status']='verified' if any(b['status']=='verified' for b in boards) else ('review' if boards else 'no_ats_found')
        unresolved=[b for b in boards if b['status']!='verified']
        comparisons=[]
        # Compare the actual API results, even for unverified candidates; label these
        # provisional, since neither similar titles nor the comparison prove ownership.
        for i,(left,ljobs) in enumerate(successful_probes):
            for right,rjobs in successful_probes[i+1:]:
                equal,diff=compare_results(left,ljobs,right,rjobs)
                diff['provisional_unverified']=left['status']!='verified' or right['status']!='verified'
                if diff['provisional_unverified']:
                    diff['impact']='Provisional comparison: at least one employer match is unverified; its jobs are withheld and closures are blocked.'
                comparisons.append(diff)
        with conn.transaction():
            save_company(conn,c)
            # Each board gets its own review item and can be approved/rejected independently.
            for b in unresolved:
                key=f"source_candidate:{c['id']}:{b['provider']}:{b.get('region','global')}:{b['token']}"
                review(conn,run_id,day,c['id'],'source_candidate',key,
                       {'message':'Candidate board is not ownership proof; do not import it without stronger evidence.',
                        'board':{'url':b['board_url'],'api_url':b['api_url'],'provider':b['provider'],
                                 'probe':b.get('probe'),'evidence':b.get('evidence')},
                        'comparisons':[x for x in comparisons if b['board_url'] in (x['board_a'],x['board_b'])]})
            if truncated:
                review(conn,run_id,day,c['id'],'discovery_truncated',f"discovery_truncated:{c['id']}",
                       {'message':'More than eight candidates exist; discovery was bounded and closure is blocked pending review.'})
    return stats

def comparable(j):
    """Strict comparison requires same posting URL and comparable description, not only title."""
    u=normalized_url(j['job_url']); title=name_key(j['title']); loc=name_key(j.get('location'))
    desc=' '.join(str(j.get('description') or '').split())
    if not u or not title or not loc or not desc or not url_has_job_id(u,j.get('external_id')): return None
    return {'url':u,'title':title,'location':loc,'employment_type':name_key(j.get('employment_type')),
            'workplace_type':name_key(j.get('workplace_type')),'description_sha256':hashlib.sha256(desc.encode()).hexdigest(),
            'extra_sha256':json_hash(j.get('comparison_extra') or {})}

def compare_results(a_board,a,b_board,b):
    a_by={normalized_url(j['job_url']):j for j in a}; b_by={normalized_url(j['job_url']):j for j in b}
    duplicate_url=(len(a_by)!=len(a) or len(b_by)!=len(b))
    a_keys=set(a_by); b_keys=set(b_by)
    changed=[]
    for key in sorted(a_keys & b_keys):
        x,y=comparable(a_by[key]),comparable(b_by[key])
        if x is None or y is None or x!=y:
            fields=['insufficient_comparable_details'] if x is None or y is None else [f for f in x if x[f]!=y[f]]
            changed.append({'url':key,'title_a':a_by[key]['title'],'title_b':b_by[key]['title'],
                            'location_a':a_by[key].get('location'),'location_b':b_by[key].get('location'),'fields':fields})
    def summary(j):
        return {'title':j['title'],'location':j.get('location'),'employment_type':j.get('employment_type'),
                'workplace_type':j.get('workplace_type'),'url':j['job_url']}
    only_a=[summary(a_by[k]) for k in sorted(a_keys-b_keys)]
    only_b=[summary(b_by[k]) for k in sorted(b_keys-a_keys)]
    # Similar titles/locations are useful review clues, NEVER evidence of job identity.
    ai=defaultdict(list); bi=defaultdict(list)
    for j in a:
        if j.get('location'): ai[(name_key(j['title']),name_key(j['location']))].append(j)
    for j in b:
        if j.get('location'): bi[(name_key(j['title']),name_key(j['location']))].append(j)
    possible=[]
    for k in sorted(set(ai)&set(bi)):
        if len(ai[k])==len(bi[k])==1:
            x,y=ai[k][0],bi[k][0]
            fields=[f for f in ('employment_type','workplace_type','description') if (x.get(f) or '')!=(y.get(f) or '')]
            possible.append({'title':x['title'],'location':x.get('location'),'url_a':x['job_url'],'url_b':y['job_url'],
                             'different_fields':fields})
    equal=bool(a and b) and not duplicate_url and not only_a and not only_b and not changed
    return equal, {'board_a':a_board['board_url'],'count_a':len(a),'board_b':b_board['board_url'],'count_b':len(b),
                   'only_a_count':len(only_a),'only_b_count':len(only_b),'changed_count':len(changed),
                   'only_a_examples':only_a[:10],'only_b_examples':only_b[:10],'changed_examples':changed[:10],
                   'possible_title_location_matches':len(possible),'possible_match_examples':possible[:10],
                   'comparison_note':'Only-A/B counts are based on posting URLs; same title/location is only a clue, not identity proof.',
                   'duplicate_urls':duplicate_url,
                   'impact':'Conflicting or insufficient job details: existing jobs are not closed and this company is held for review.'}

def source_ref(board,job):
    return {'provider':board['provider'],'region':board.get('region','global'),'board_url':board['board_url'],
            'token':board['token'],'external_id':str(job['external_id'])}
def ref_key(ref):
    return '|'.join([ref['provider'],ref['region'],ref['token'],ref['external_id']])

def apply_job(conn,c,board,j,day,extra_refs=(),prefer_current=False):
    ref=source_ref(board,j); sk=ref_key(ref); refs=[ref]+list(extra_refs)
    found={}
    for candidate_ref in refs:
        stable_ref={k:candidate_ref[k] for k in ('provider','region','token','external_id')}
        for row in conn.execute('SELECT * FROM public.jobs WHERE source_key=%s OR job_sources @> %s::jsonb',
                                (ref_key(candidate_ref),Jsonb([stable_ref]))).fetchall():
            found[str(row['id'])]=row
    if len(found)>1: raise ValueError('Equivalent source identities are attached to multiple jobs')
    existing=next(iter(found.values()),None)
    if existing and existing['company_id']!=c['id']:
        raise ValueError('Posting source is already owned by another company')
    if not existing:
        same_url=conn.execute('SELECT * FROM public.jobs WHERE company_id=%s AND canonical_url=%s',
                              (c['id'],normalized_url(j['job_url']))).fetchall()
        if same_url:
            same_board=[x for x in same_url if any(p.get('provider')==ref['provider'] and p.get('region','global')==ref['region']
                                                    and p.get('token')==ref['token'] for p in x['job_sources'])]
            if same_board:
                # Different native IDs from the SAME board are separate listings,
                # even if that provider reused a generic URL or an identical title.
                if len(same_board)!=len(same_url): raise ValueError('Ambiguous cross-board posting URL')
            else:
                # Exact job-specific URL plus compatible details; never title alone.
                compatible=[x for x in same_url if name_key(x['title'])==name_key(j['title']) and x['location'] and j.get('location')
                            and name_key(x['location'])==name_key(j['location']) and (not x['employment_type'] or not j.get('employment_type') or name_key(x['employment_type'])==name_key(j['employment_type']))
                            and (url_has_job_id(j['job_url'],j['external_id']) or url_has_job_id(j['job_url'],x['external_id']))]
                if len(compatible)!=1 or len(same_url)!=1: raise ValueError('Same posting URL has ambiguous job details; review rather than merge')
                existing=compatible[0]
    raw_hash=json_hash(j['raw'])
    if not existing:
        row=conn.execute('''INSERT INTO public.jobs(company_id,source_key,provider,board_url,external_id,title,location,employment_type,workplace_type,
              job_url,canonical_url,requisition_id,date_posted,published_at_source,first_seen_on,last_seen_on,data_hash,raw_data,job_sources,history)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
              (c['id'],sk,board['provider'],board['board_url'],j['external_id'],j['title'],j.get('location') or None,
               j.get('employment_type') or None,j.get('workplace_type') or None,j['job_url'],normalized_url(j['job_url']),
               j.get('requisition_id') or None,j.get('date_posted') or None,j.get('published_at_source') or None,
               day,day,raw_hash,Jsonb(j['raw']),Jsonb(refs),Jsonb([{'event':'created','day':str(day)}]))).fetchone()
        return row['id'],'new'
    prov=list(existing['job_sources'])
    for r in refs:
        if r not in prov: prov.append(r)
    history=list(existing['history']); event=[]
    if existing['status']=='unavailable': history.append({'event':'returned','day':str(day)}); event.append('returned')
    primary=(existing['source_key']==sk or prefer_current)
    if prefer_current and existing['source_key']!=sk:
        history.append({'event':'primary_source_changed','day':str(day),'from':existing['source_key'],'to':sk})
    if primary and existing['data_hash']!=raw_hash:
        changes={}
        for key,col in [('title','title'),('location','location'),('employment_type','employment_type'),('workplace_type','workplace_type'),('job_url','job_url')]:
            old=existing[col] or ''; new=j.get(key) or ''
            if old!=new: changes[key]={'old':old,'new':new}
        history.append({'event':'updated','day':str(day),'changed_fields':changes,'old_hash':existing['data_hash'],'new_hash':raw_hash}); event.append('updated')
    # If this is an equivalent secondary source, attach its identity but do not overwrite the primary raw record.
    if primary:
        conn.execute('''UPDATE public.jobs SET source_key=%s,provider=%s,board_url=%s,external_id=%s,
          title=%s,location=%s,employment_type=%s,workplace_type=%s,job_url=%s,
          canonical_url=%s,requisition_id=%s,date_posted=%s,published_at_source=%s,status='available',unavailable_on=NULL,
          last_seen_on=%s,data_hash=%s,raw_data=%s,job_sources=%s,history=%s,updated_at=now() WHERE id=%s''',
          (sk,board['provider'],board['board_url'],j['external_id'],j['title'],j.get('location') or None,
           j.get('employment_type') or None,j.get('workplace_type') or None,j['job_url'],
           normalized_url(j['job_url']),j.get('requisition_id') or None,j.get('date_posted') or None,j.get('published_at_source') or None,
           day,raw_hash,Jsonb(j['raw']),Jsonb(prov),Jsonb(history),existing['id']))
    else:
        conn.execute("UPDATE public.jobs SET status='available',unavailable_on=NULL,last_seen_on=%s,job_sources=%s,history=%s,updated_at=now() WHERE id=%s",
                     (day,Jsonb(prov),Jsonb(history),existing['id']))
    return existing['id'],'+'.join(event) or 'unchanged'

def mark_absent(conn, company_id, seen, day, active_boards):
    rows=conn.execute("SELECT id,history,job_sources FROM public.jobs WHERE company_id=%s AND status='available'",(company_id,)).fetchall()
    active={(b['provider'],b.get('region','global'),b['token']) for b in active_boards}
    missing=[row for row in rows if str(row['id']) not in seen]
    uncovered=[row for row in missing if not any((p.get('provider'),p.get('region','global'),p.get('token')) in active for p in row['job_sources'])]
    # Uncovered sources create uncertainty for the company, so close nothing.
    if uncovered:
        return 0,len(uncovered)
    for row in missing:
        history=list(row['history']); history.append({'event':'unavailable','day':str(day)})
        conn.execute("UPDATE public.jobs SET status='unavailable',unavailable_on=%s,history=%s,updated_at=now() WHERE id=%s",(day,Jsonb(history),row['id']))
    return len(missing),0

def refresh_company(conn,c,client,run_id,day):
    boards=[b for b in c['ats_sources'] if b.get('status')=='verified' and b.get('role')!='alternative']
    stats={'company_id':str(c['id']),'name':c['name'],'boards_checked':0,'new':0,'updated':0,'returned':0,'unavailable':0,'unchanged':0,'outcome':'skipped'}
    if not boards: return stats
    results=[]
    for b in boards:
        result=fetch_board(client,b); stats['boards_checked']+=1
        b['last_refresh']={'complete':result.complete,'count':len(result.jobs) if result.complete else None,'error':result.error,'run_day':str(day)}
        if not result.complete:
            with conn.transaction():
                save_company(conn,c)
                review(conn,run_id,day,c['id'],'ats_api_failure',f"ats_api_failure:{c['id']}:{b['provider']}:{b['token']}",
                       {'message':'Failed or incomplete response; no jobs changed.', 'board_url':b['board_url'],'api_url':b['api_url'],'error':result.error})
            stats['outcome']='incomplete_no_changes'; return stats
        if not result.jobs:
            with conn.transaction():
                save_company(conn,c)
                review(conn,run_id,day,c['id'],'ats_api_empty',f"ats_api_empty:{c['id']}:{b['provider']}:{b['token']}",
                       {'message':'Successful empty board: existing jobs remain unchanged; manual review required.', 'board_url':b['board_url'],'api_url':b['api_url'],'count':0})
            stats['outcome']='empty_no_changes'; return stats
        results.append((b,result.jobs))
    accepted=sorted(c.get('board_policy',{}).get('accepted_board_urls') or [])
    policy=c.get('board_policy',{}).get('mode')
    extra_map={}; selected=results
    if len(results)>1:
        is_both=policy=='both' and accepted==sorted(b['board_url'] for b,_ in results)
        if not is_both:
            first,first_jobs=results[0]; all_same=True; differences=[]
            for other,other_jobs in results[1:]:
                equal,diff=compare_results(first,first_jobs,other,other_jobs)
                all_same=all_same and equal; differences.append(diff)
            if not all_same:
                with conn.transaction():
                    save_company(conn,c)
                    review(conn,run_id,day,c['id'],'source_conflict',f"source_conflict:{c['id']}",
                           {'message':'Verified sources differ or do not contain enough details to prove equality.', 'comparisons':differences})
                stats['outcome']='source_conflict_no_changes'; return stats
            # Deterministic primary; preserve matched source identities. Do not poll equivalent alternatives daily.
            results=sorted(results,key=lambda x:(x[0]['provider'],x[0]['board_url']))
            primary,pjobs=results[0]; selected=[results[0]]
            for alt,ajobs in results[1:]:
                alt['role']='alternative'
                alt_by_url={normalized_url(j['job_url']):j for j in ajobs}
                for j in pjobs:
                    extra_map.setdefault(normalized_url(j['job_url']),[]).append(source_ref(alt,alt_by_url[normalized_url(j['job_url'])]))
            primary['role']='primary'
    blocking=conn.execute("SELECT count(*) AS n FROM public.sync_logs WHERE company_id=%s AND kind='review' AND status='pending' AND phase IN ('source_candidate','source_conflict','company_identity','discovery_truncated','job_identity','uncovered_source')",(c['id'],)).fetchone()['n']>0
    try:
        with conn.transaction():
            seen=set()
            for b,jobs in selected:
                for j in jobs:
                    jid,action=apply_job(conn,c,b,j,day,extra_map.get(normalized_url(j['job_url']),[]),prefer_current=(len(selected)==1))
                    seen.add(str(jid))
                    if action=='new': stats['new']+=1
                    elif action=='unchanged': stats['unchanged']+=1
                    else:
                        if 'updated' in action: stats['updated']+=1
                        if 'returned' in action: stats['returned']+=1
            if not blocking:
                stats['unavailable'],uncovered=mark_absent(conn,c['id'],seen,day,[b for b,_ in selected])
                if uncovered:
                    review(conn,run_id,day,c['id'],'uncovered_source',f"uncovered_source:{c['id']}",
                           {'message':'Some existing jobs came from a source not refreshed authoritatively; those jobs were not closed.',
                            'uncovered_jobs':uncovered,'active_boards':[b['board_url'] for b,_ in selected]})
            save_company(conn,c)
        stats['outcome']='updated_closure_blocked_by_review' if blocking else 'updated'
    except (ValueError,TypeError) as exc:
        # A transaction rolled back all job changes for this company.
        for k in ('new','updated','returned','unavailable','unchanged'): stats[k]=0
        with conn.transaction():
            save_company(conn,c)
            review(conn,run_id,day,c['id'],'job_identity',f"job_identity:{c['id']}",
                   {'message':'Ambiguous job identity; no job changes were committed.','error':str(exc)[:250]})
        stats['outcome']='identity_conflict_no_changes'
    return stats

def refresh_all(conn,client,run_id,day):
    results=[]
    for c in conn.execute('SELECT * FROM public.companies ORDER BY created_at,id').fetchall():
        try:
            results.append(refresh_company(conn,c,client,run_id,day))
        except Exception as exc:
            review(conn,run_id,day,c['id'],'refresh_error',f"refresh_error:{c['id']}",
                   {'message':'Unexpected refresh error; no further work for this company.','error':f'{type(exc).__name__}: {str(exc)[:250]}'})
            results.append({'company_id':str(c['id']),'name':c['name'],'outcome':'error','error':str(exc)[:250], 'boards_checked':0,'new':0,'updated':0,'returned':0,'unavailable':0,'unchanged':0})
    return results

def resolve_review(conn, review_id, decision):
    """Trusted administrator operation; never invoked to manufacture live simulation results."""
    with conn.transaction():
        if not conn.execute('SELECT pg_try_advisory_xact_lock(%s) AS ok',(LOCK_KEY,)).fetchone()['ok']:
            raise RuntimeError('Another pipeline run is already active')
        item=conn.execute("SELECT * FROM public.sync_logs WHERE id=%s AND kind='review' AND status='pending' FOR UPDATE",(review_id,)).fetchone()
        if not item: raise ValueError('Pending review not found')
        mode=decision.get('mode')
        if item['phase']=='source_candidate':
            if mode not in ('approve','reject') or not str(decision.get('reason') or '').strip():
                raise ValueError('Candidate approval/rejection requires a reason')
            c=conn.execute('SELECT * FROM public.companies WHERE id=%s FOR UPDATE',(item['company_id'],)).fetchone()
            url=item['details'].get('board',{}).get('url')
            candidates=[b for b in c['ats_sources'] if b.get('board_url')==url and b.get('status')=='candidate']
            if len(candidates)!=1: raise ValueError('Candidate board no longer matches review')
            b=candidates[0]
            if mode=='approve':
                for other in conn.execute('SELECT id,ats_sources FROM public.companies WHERE id<>%s',(c['id'],)).fetchall():
                    if any(x.get('status')=='verified' and x.get('provider')==b['provider'] and x.get('token')==b['token']
                           and x.get('region','global')==b.get('region','global') for x in other['ats_sources']):
                        raise ValueError('Board is already assigned to a different company')
            b['status']='verified' if mode=='approve' else 'rejected'
            b.setdefault('evidence',{})['manual_resolution']={'decision':mode,'reason':decision['reason']}
            c['ats_status']='verified' if any(x.get('status')=='verified' for x in c['ats_sources']) else 'review'
            save_company(conn,c)
        elif item['phase']=='source_conflict':
            if mode not in ('both','primary'): raise ValueError('Choose both or a primary source')
            c=conn.execute('SELECT * FROM public.companies WHERE id=%s FOR UPDATE',(item['company_id'],)).fetchone()
            verified=[b for b in c['ats_sources'] if b.get('status')=='verified' and b.get('role')!='alternative']
            urls=sorted(b['board_url'] for b in verified)
            if len(urls)<2: raise ValueError('Need two verified boards')
            if mode=='both':
                pass
            else:
                chosen=decision.get('board_url')
                if chosen not in urls: raise ValueError('Primary URL must be one of the reviewed boards')
                for b in verified: b['role']='primary' if b['board_url']==chosen else 'alternative'
                pass
            save_company(conn,c)
        elif mode!='acknowledge':
            raise ValueError('Use acknowledge for this review type')
        conn.execute("UPDATE public.sync_logs SET status='resolved',details=details || %s::jsonb,updated_at=now() WHERE id=%s",
                     (Jsonb({'resolution':decision}),review_id))

def run_day(conn, day, feed_loader, catalog, client):
    if not conn.autocommit: raise ValueError('Use autocommit with explicit short transactions')
    locked=conn.execute('SELECT pg_try_advisory_lock(%s) AS ok',(LOCK_KEY,)).fetchone()['ok']
    if not locked: raise RuntimeError('Another pipeline run is already active')
    run_id=uuid.uuid4(); errors=[]; out={'run_id':str(run_id),'run_day':str(day),'feed_reads':0}
    try:
        conn.execute("INSERT INTO public.sync_logs(id,run_id,run_day,kind,phase,status,details) VALUES (%s,%s,%s,'run','daily','running',%s)",
                     (run_id,run_id,day,Jsonb({})))
        # Even a failed onboarding step must not prevent existing approved-board refresh.
        try:
            out['feed_reads']+=1; feed=feed_loader()
            out['onboarding']=onboard(conn,feed,run_id,day)
        except Exception as exc:
            errors.append({'phase':'onboarding','error':f'{type(exc).__name__}: {str(exc)[:300]}'})
            review(conn,run_id,day,None,'onboarding_failure',f'onboarding_failure:{day}',
                   {'message':'Feed/onboarding failed; approved-board refresh still runs.','error':str(exc)[:300]})
            out['onboarding']={'new_companies':0,'new_company_ids':[],'error':str(exc)[:300]}
        try:
            out['discovery']=discover_pending(conn,catalog,client,run_id,day)
        except Exception as exc:
            errors.append({'phase':'discovery','error':f'{type(exc).__name__}: {str(exc)[:300]}'})
            review(conn,run_id,day,None,'discovery_failure',f'discovery_failure:{day}',
                   {'message':'Discovery failed; approved-board refresh still runs.','error':str(exc)[:300]})
            out['discovery']={'error':str(exc)[:300]}
        try:
            out['refresh']=refresh_all(conn,client,run_id,day)
        except Exception as exc:
            errors.append({'phase':'refresh','error':f'{type(exc).__name__}: {str(exc)[:300]}'})
            out['refresh']=[]
        out['errors']=errors
        out['request_events']=list(client.events)
        out['pending_review_items']=conn.execute("SELECT count(*) AS n FROM public.sync_logs WHERE kind='review' AND status='pending'").fetchone()['n']
        out['companies_total']=conn.execute('SELECT count(*) AS n FROM public.companies').fetchone()['n']
        out['jobs_available']=conn.execute("SELECT count(*) AS n FROM public.jobs WHERE status='available'").fetchone()['n']
        out['jobs_unavailable']=conn.execute("SELECT count(*) AS n FROM public.jobs WHERE status='unavailable'").fetchone()['n']
        outcomes=[r.get('outcome') for r in out['refresh']]
        out['status']='partial' if errors or out['pending_review_items'] or any(x in ('error','incomplete_no_changes','empty_no_changes','source_conflict_no_changes','identity_conflict_no_changes') for x in outcomes) else 'ok'
        conn.execute("UPDATE public.sync_logs SET status=%s,details=%s,updated_at=now() WHERE id=%s",(out['status'],Jsonb(out),run_id))
        return out
    except Exception as exc:
        conn.execute("UPDATE public.sync_logs SET status='failed',details=%s,updated_at=now() WHERE id=%s",
                     (Jsonb({'fatal':f'{type(exc).__name__}: {str(exc)[:300]}'}),run_id))
        raise
    finally:
        conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_KEY,))
