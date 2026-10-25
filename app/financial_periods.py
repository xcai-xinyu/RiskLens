"""period-v1: source-bound period equivalence, never a global FY -> December rule."""
import hashlib
import json
import re
import time
from datetime import date

VERSION = 'source-bound-period-v1'
STOCK = {'current_ratio', 'cash_ratio', 'debt_to_equity'}
FLOW = {'interest_coverage', 'operating_cash_flow_change'}


def parse_date(text):
    text = text.strip().replace('\u00a0', ' ')
    for fmt in ('%Y-%m-%d', '%B %d, %Y', '%b %d, %Y', '%Y年%m月%d日'):
        try:
            return date(*time.strptime(text, fmt)[:3]).isoformat()
        except ValueError:
            continue
    return None


def fiscal_metadata(sources):
    """Only original source cover metadata. No labels, questions or answer files.

    A cover establishes the fiscal end, not the start/duration. Start remains
    unknown unless separately evidenced; a 52/53-week year is never guessed.
    """
    rows = []
    for source in sources:
        source.verify()
        pages = json.loads(source.parsed_path.read_text())
        page = pages[0]
        match = re.search(r'(?i)For the fiscal year ended\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})', page['text'])
        if not match or not parse_date(match[1]):
            continue
        end = parse_date(match[1])
        ingested_hash = source.raw_sha256
        if source.raw_path.suffix == '.docx':
            ingested_hash = hashlib.sha256('\n\n'.join(p['text'] for p in pages).encode()).hexdigest()
        runtime_id = 'doc-' + hashlib.sha256((source.company_group + ':' + ingested_hash).encode()).hexdigest()[:24]
        rows.append({'company': source.company, 'fiscal_label': 'FY:'+end[:4],
            'start_date': None, 'end_date': end, 'document_id': source.document_id,
            'source_group_id': source.source_group_id, 'source_hash': source.raw_sha256,
            'parsed_hash': source.parsed_sha256, 'published_at': source.published_at,
            'runtime_document_id': runtime_id, 'ingested_hash': ingested_hash,
            'source_location': {'file_page': page['file_page'], 'section': 'Form 10-K cover',
                                'offset': match.start()},
            'quote': match[0], 'start_date_status': 'NOT_ESTABLISHED_BY_COVER'})
    return rows


def normalize(raw, *, company, metric_name, metadata, document_ids):
    kind = 'instant' if metric_name in STOCK else 'duration' if metric_name in FLOW else 'unknown'
    result = {'version': VERSION, 'raw': raw, 'company': company, 'kind': kind,
              'as_of_date': None, 'start_date': None, 'end_date': None,
              'fiscal_label': None, 'basis': [], 'status': 'UNKNOWN'}
    text = raw.strip()
    fiscal = re.fullmatch(r'(?i)FY\s*:?\s*(\d{4})', text)
    if fiscal:
        label = 'FY:'+fiscal[1]
        result['fiscal_label'] = label
        rows = [r for r in metadata if r['company'].casefold() == company.casefold()
                and r['fiscal_label'] == label and ({r['document_id'], r.get('runtime_document_id')} & document_ids)
                and r.get('quote') and re.fullmatch(r'[a-f0-9]{64}', r.get('source_hash', ''))]
        # Conflicting fiscal metadata cannot establish an equivalence.
        if rows and len({(r.get('start_date'), r['end_date']) for r in rows}) == 1:
            result['basis'] = rows
            if kind == 'instant':
                result.update(as_of_date=rows[0]['end_date'], status='RESOLVED')
            elif kind == 'duration':
                result.update(start_date=rows[0].get('start_date'), end_date=rows[0]['end_date'])
                if result['start_date']:
                    result['status'] = 'RESOLVED'
        return result
    interval = re.fullmatch(r'(\d{4}-\d{2}-\d{2})\s*(?:/|to)\s*(\d{4}-\d{2}-\d{2})', text)
    if interval and kind == 'duration':
        start, end = parse_date(interval[1]), parse_date(interval[2])
        if start and end and start <= end:
            result.update(start_date=start, end_date=end, status='RESOLVED')
        return result
    instant = parse_date(re.sub(r'(?i)^as of\s+', '', text))
    if instant:
        # A bare end date is not a flow interval.
        result.update(kind='instant', as_of_date=instant, status='RESOLVED')
        return result
    ended = re.fullmatch(r'(?i)(?:year|fiscal year) ended\s+(.+)', text)
    if ended and parse_date(ended[1]):
        end = parse_date(ended[1])
        result.update(kind='duration', end_date=end)
        rows = [r for r in metadata if r['company'].casefold() == company.casefold()
                and ({r['document_id'], r.get('runtime_document_id')} & document_ids)
                and r['end_date'] == end and r.get('start_date')]
        if rows and len({r['start_date'] for r in rows}) == 1:
            result.update(start_date=rows[0]['start_date'], basis=rows, status='RESOLVED')
    return result


def equivalent(left, right, **context):
    a, b = normalize(left, **context), normalize(right, **context)
    expected_kind = 'instant' if context['metric_name'] in STOCK else 'duration'
    if a['kind'] != b['kind'] or a['kind'] != expected_kind:
        status = 'MISMATCH'
    elif left == right:
        status = 'MATCH'  # Existing exact input-period identity remains usable.
    elif a['status'] != 'RESOLVED' or b['status'] != 'RESOLVED':
        status = 'UNKNOWN'
    else:
        fields = ('as_of_date',) if a['kind'] == 'instant' else ('start_date', 'end_date')
        status = 'MATCH' if all(a[k] == b[k] for k in fields) else 'MISMATCH'
    return {'status': status, 'left': a, 'right': b,
            'trace_hash': hashlib.sha256(json.dumps([a,b], sort_keys=True).encode()).hexdigest()}


def usable_metadata(rows, cutoff):
    return [r for r in rows if date.fromisoformat(r['published_at']) <= cutoff]
