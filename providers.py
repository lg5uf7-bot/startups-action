"""Adapters for documented public listing APIs; no HTML page fetching or location/type filters."""
from __future__ import annotations
from dataclasses import dataclass
from urllib.parse import urlsplit, quote
from discovery import board_urls, valid_http_url
from net import FetchError

class IncompleteBoard(Exception): pass
@dataclass
class BoardResult:
    complete: bool
    jobs: list
    error: str = ''
    pages: int = 0

def _text(v): return str(v).strip() if v is not None else ''
def _loc(v):
    if isinstance(v,dict):
        for key in ('name','label','fullLocation'):
            if isinstance(v.get(key),str) and v[key].strip(): return v[key]
        return ', '.join(str(v.get(k)) for k in ('city','region','country') if v.get(k))
    if isinstance(v,list): return ', '.join(str(x) for x in v)
    return _text(v)

def _finish(job):
    if not job.get('external_id') or not job.get('title') or not valid_http_url(job.get('job_url')):
        raise IncompleteBoard('Missing stable ID, title or valid posting URL; entire response withheld')
    if urlsplit(job['job_url']).scheme!='https':
        raise IncompleteBoard('Posting URL is not HTTPS; entire response withheld')
    return job

def normalize_gh(row):
    if not isinstance(row,dict): raise IncompleteBoard('Invalid job object')
    return _finish({'external_id':_text(row.get('id')), 'title':_text(row.get('title')),
        'location':_loc(row.get('location')), 'employment_type':_text(row.get('employment_type')),
        'workplace_type':_text(row.get('workplace_type')), 'job_url':_text(row.get('absolute_url')),
        'requisition_id':_text(row.get('requisition_id')), 'date_posted':_text(row.get('first_published')),
        'published_at_source':'', 'description':_text(row.get('content')),
        'comparison_extra':{'offices':row.get('offices'),'departments':row.get('departments'),'pay_input_ranges':row.get('pay_input_ranges')}, 'raw':row})

def normalize_ashby(row):
    if not isinstance(row,dict): raise IncompleteBoard('Invalid job object')
    u=_text(row.get('jobUrl')); parts=urlsplit(u).path.strip('/').split('/')
    # The hosted job URL path includes board name + stable posting id.
    ident=parts[-1] if len(parts)>=2 else ''
    return _finish({'external_id':ident, 'title':_text(row.get('title')), 'location':_loc(row.get('location')),
        'employment_type':_text(row.get('employmentType')), 'workplace_type':_text(row.get('workplaceType')),
        'job_url':u, 'requisition_id':_text(row.get('requisitionId')), 'date_posted':'',
        'published_at_source':_text(row.get('publishedAt')), 'description':_text(row.get('descriptionPlain') or row.get('descriptionHtml')),
        'comparison_extra':{'secondaryLocations':row.get('secondaryLocations'),'compensation':row.get('compensation'),'isRemote':row.get('isRemote')}, 'raw':row})

def normalize_lever(row):
    if not isinstance(row,dict): raise IncompleteBoard('Invalid job object')
    cat=row.get('categories') or {}
    if not isinstance(cat,dict): raise IncompleteBoard('Invalid Lever categories')
    # createdAt is created time; not automatically first published.
    return _finish({'external_id':_text(row.get('id')), 'title':_text(row.get('text')),
        'location':_loc(cat.get('location')), 'employment_type':_text(cat.get('commitment')),
        'workplace_type':_text(row.get('workplaceType')), 'job_url':_text(row.get('hostedUrl')),
        'requisition_id':_text(row.get('requisitionId')), 'date_posted':'', 'published_at_source':'',
        'description':_text(row.get('descriptionPlain') or row.get('description')),
        'comparison_extra':{'allLocations':cat.get('allLocations'),'team':cat.get('team'),'salaryRange':row.get('salaryRange')}, 'raw':row})

def normalize_gem(row):
    if not isinstance(row,dict): raise IncompleteBoard('Invalid job object')
    return _finish({'external_id':_text(row.get('id')), 'title':_text(row.get('title')), 'location':_loc(row.get('location')),
        'employment_type':_text(row.get('employment_type')), 'workplace_type':_text(row.get('location_type')),
        'job_url':_text(row.get('absolute_url')), 'requisition_id':_text(row.get('requisition_id')),
        'date_posted':_text(row.get('first_published_at')), 'published_at_source':'',
        'description':_text(row.get('content_plain') or row.get('content')),
        'comparison_extra':{'offices':row.get('offices'),'departments':row.get('departments')}, 'raw':row})

def normalize_smart(row, token):
    if not isinstance(row,dict): raise IncompleteBoard('Invalid job object')
    ident=_text(row.get('id')); hosted=_text(row.get('applyUrl') or row.get('postingUrl') or row.get('jobUrl') or f"https://jobs.smartrecruiters.com/{token}/{ident}")
    return _finish({'external_id':ident, 'title':_text(row.get('name')),
        'location':_loc(row.get('location')), 'employment_type':_loc(row.get('typeOfEmployment')),
        'workplace_type':_text(row.get('locationType')), 'job_url':hosted,
        'requisition_id':_text(row.get('refNumber')), 'date_posted':_text(row.get('releasedDate')),
        'published_at_source':'', 'description':_text(row.get('description')),
        'comparison_extra':{'location':row.get('location'),'department':row.get('department')}, 'raw':row})

def get_metadata(client, board):
    if board['provider']!='greenhouse': return {}
    url='https://boards-api.greenhouse.io/v1/boards/'+quote(board['token'],safe='-_')
    d=client.get(url,'board_metadata',cache=True)
    if not isinstance(d,dict) or not isinstance(d.get('name'),str):
        raise IncompleteBoard('Malformed Greenhouse board metadata')
    return d

def fetch_board(client, board):
    p=board['provider']; token=board['token']; region=board.get('region','global')
    _,url=board_urls(p,token,region)
    if not url: return BoardResult(False,[],f'Unsupported provider {p}')
    jobs=[]; seen=set(); pages=0
    try:
        if p in ('greenhouse','ashby','gem'):
            data=client.get(url,'board_jobs',cache=True); pages=1
            if p=='gem':
                if not isinstance(data,list): raise IncompleteBoard('Gem response is not an array')
                rows=data; normalize=normalize_gem
            else:
                if not isinstance(data,dict) or not isinstance(data.get('jobs'),list): raise IncompleteBoard('Expected a jobs array')
                rows=data['jobs']; normalize=normalize_gh if p=='greenhouse' else normalize_ashby
                if p=='greenhouse':
                    meta=data.get('meta')
                    if not isinstance(meta,dict) or not isinstance(meta.get('total'),int) or meta['total']!=len(rows):
                        raise IncompleteBoard('Greenhouse total missing or differs from returned list')
            for row in rows:
                if p=='ashby' and isinstance(row,dict) and row.get('isListed') is False:
                    continue # explicitly not publicly listed by the provider
                j=normalize(row)
                if p=='ashby':
                    parts=urlsplit(j['job_url']).path.strip('/').split('/')
                    if parts[0].casefold()!=token.casefold(): raise IncompleteBoard('Ashby posting board token mismatch')
                if j['external_id'] in seen: raise IncompleteBoard('Duplicate job identifier')
                seen.add(j['external_id']); jobs.append(j)
        elif p in ('lever','smartrecruiters'):
            for offset in range(0,10000,100):
                u=url.rsplit('=',1)[0]+'='+str(offset)
                data=client.get(u,'board_jobs',cache=True); pages+=1
                if p=='lever':
                    if not isinstance(data,list): raise IncompleteBoard('Lever page is not an array')
                    rows=data; total=None
                else:
                    if not isinstance(data,dict) or not isinstance(data.get('content'),list): raise IncompleteBoard('SmartRecruiters page is malformed')
                    rows=data['content']; total=data.get('totalFound')
                    if not isinstance(total,int) or total<0: raise IncompleteBoard('SmartRecruiters total missing')
                if len(rows)>100: raise IncompleteBoard('Page larger than requested limit')
                for row in rows:
                    j=normalize_lever(row) if p=='lever' else normalize_smart(row,token)
                    if j['external_id'] in seen: raise IncompleteBoard('Duplicate ID across pages; possible pagination drift')
                    seen.add(j['external_id']); jobs.append(j)
                if p=='lever' and len(rows)<100: break
                if p=='smartrecruiters':
                    if len(jobs)>total: raise IncompleteBoard('Total inconsistent')
                    if len(jobs)==total: break
                    if len(rows)<100: raise IncompleteBoard('Short page before reported total')
            else: raise IncompleteBoard('Pagination safety limit reached')
        else:
            raise IncompleteBoard('Unsupported documented provider')
        return BoardResult(True,jobs,'',pages)
    except (FetchError,IncompleteBoard,ValueError,TypeError) as exc:
        return BoardResult(False,[],str(exc)[:300],pages)
 
