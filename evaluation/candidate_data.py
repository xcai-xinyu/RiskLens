"""Versioned evaluator data and deliberately smaller, source-attested runtime jobs."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.schemas import ResearchRequest

SCORING_VERSION = 'p4-point-review-v1'
DATA_VERSION = 'p4-v3-candidate-002'
FINALIZATION_SCORING_VERSION = 'p4-point-review-v2'
FINALIZATION_DATA_VERSION = 'p4-v3-candidate-003'
CANDIDATE_VERSIONS = {DATA_VERSION: SCORING_VERSION,
                      FINALIZATION_DATA_VERSION: FINALIZATION_SCORING_VERSION}
REQUEST_FIELDS = frozenset({'company', 'question', 'as_of_date', 'risk_categories', 'mode'})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def request_only(value):
    """Select fields explicitly even if an evaluator accidentally passes a larger object."""
    if isinstance(value, ResearchRequest):
        value = value.model_dump(mode='json')
    return ResearchRequest.model_validate({key: value[key] for key in REQUEST_FIELDS})


def jsonlines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


@dataclass(frozen=True)
class RawSource:
    document_id: str
    company: str
    company_group: str
    source_group_id: str
    title: str
    url: str
    published_at: str
    raw_path: Path
    raw_sha256: str
    parsed_path: Path
    parsed_sha256: str

    def verify(self):
        raw = self.raw_path.resolve()
        parsed = self.parsed_path.resolve()
        # An explicit catalog is required; directories are never scanned for content.
        if (raw.parent.name != 'raw' or raw.parent.parent.name != 'sources'
                or parsed.parent != raw.parent.parent / 'parsed'
                or raw.suffix not in {'.pdf', '.docx', '.txt', '.html', '.htm'}
                or parsed.suffix != '.json'):
            raise ValueError('unapproved_source_path')
        if file_hash(raw) != self.raw_sha256 or file_hash(parsed) != self.parsed_sha256:
            raise ValueError('source_attestation_mismatch')
        if any(part in {'reviewer_only', 'review', 'proposals', 'audit'} for part in raw.parts):
            raise ValueError('evaluator_material_not_source')


@dataclass(frozen=True)
class RuntimeJob:
    case_id: str
    request: ResearchRequest
    sources: tuple[RawSource, ...]


@dataclass
class CandidateDataset:
    manifest: dict
    cases: dict
    requests: dict
    evidence: dict
    sources: tuple[RawSource, ...]

    def runtime_job(self, case_id):
        # Selection uses the question/metadata file, never evidence IDs or Gold values.
        row = self.requests[case_id]
        request = request_only(row['request'])
        selected = tuple(s for s in self.sources if s.company == request.company
                         and s.document_id in row['document_ids']
                         and s.published_at <= request.as_of_date.isoformat())
        for source in selected:
            source.verify()
        return RuntimeJob(case_id, request, selected)


def validate_case(case):
    if case.get('scoring_version') not in CANDIDATE_VERSIONS.values():
        raise ValueError('candidate_scoring_version')
    points = case['obligations']
    if not points or len({p['point_id'] for p in points}) != len(points):
        raise ValueError('duplicate_or_empty_obligations')
    facts = {f['fact_id'] for f in case['facts']}
    for point in points:
        if point['kind'] not in {'fact', 'premise', 'analysis', 'boundary', 'gap', 'prohibition'}:
            raise ValueError('unknown_point_kind')
        if not point['requirement'] or not isinstance(point['completeness'], bool):
            raise ValueError('invalid_point')
        if not set(point['premise_fact_ids']) <= facts:
            raise ValueError('missing_analysis_premise')
    if not any(p['kind'] in {'analysis', 'boundary'} and p['completeness'] for p in points):
        raise ValueError('missing_required_analysis')
    group_ids = [g['group_id'] for g in case['metric_groups']]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError('duplicate_metric_group')
    for group in case['metric_groups']:
        if group['selection'] not in {'fixed', 'one_appropriate_definition'} or not group['references']:
            raise ValueError('invalid_metric_group')
        policy = group.get('definition_policy')
        if policy and (case['scoring_version'] != FINALIZATION_SCORING_VERSION
                       or group['selection'] != 'one_appropriate_definition'
                       or policy.get('version') != 'explained-measurement-basis-v1'
                       or policy.get('standard_reference_id') not in
                       {r['metric_id'] for r in group['references']}):
            raise ValueError('invalid_definition_policy')
    recorded = (case['scoring_version'] == FINALIZATION_SCORING_VERSION
                and case['gold_status'] == 'CANDIDATE_CONTENT_REVIEW_RECORDED'
                and case.get('content_review', {}).get('status') in
                {'PASS_CONTENT_ONLY', 'ACCEPTED_WITH_AUTHORIZED_LIMITED_FIX'}
                and case['content_review'].get('event_id')
                and case['content_review'].get('reviewed_candidate') == DATA_VERSION)
    if case['gold_status'] != 'CANDIDATE_PENDING_HUMAN_REVIEW' and not recorded:
        raise ValueError('candidate_cannot_self_approve')


def load_candidate_dataset(directory, *, project_root=None):
    directory = Path(directory).resolve()
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    if CANDIDATE_VERSIONS.get(manifest['data_version']) != manifest['scoring_version']:
        raise ValueError('candidate_version_mismatch')
    required = {'reviewer_only/annotations.jsonl', 'reviewer_only/evidence.jsonl',
                'inputs/requests.jsonl', 'inputs/source_catalog.json'}
    if not required <= set(manifest['files']):
        raise ValueError('missing_candidate_file_attestation')
    for name, expected in manifest['files'].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or file_hash(path) != expected:
            raise ValueError('candidate_file_integrity')
    cases = jsonlines(directory / 'reviewer_only/annotations.jsonl')
    inputs = jsonlines(directory / 'inputs/requests.jsonl')
    evidence = jsonlines(directory / 'reviewer_only/evidence.jsonl')
    sources = []
    for raw in json.loads((directory / 'inputs/source_catalog.json').read_text()):
        item = dict(raw)
        for key in ('raw_path', 'parsed_path'):
            item[key] = (root / item[key]).resolve()
            if not item[key].is_relative_to(root):
                raise ValueError('source_outside_project')
        source = RawSource(**item)
        source.verify()
        sources.append(source)
    by_case = {c['case_id']: c for c in cases}
    by_request = {r['case_id']: r for r in inputs}
    by_evidence = {e['evidence_id']: e for e in evidence}
    if len(by_case) != len(cases) or len(by_request) != len(inputs) or set(by_case) != set(by_request):
        raise ValueError('candidate_case_ids')
    if len(by_evidence) != len(evidence):
        raise ValueError('candidate_evidence_ids')
    for source in sources:
        pages = json.loads(source.parsed_path.read_text())
        for e in evidence:
            if e['document_id'] != source.document_id:
                continue
            page = next((p for p in pages if p['file_page'] == e['file_page']), None)
            if page is None or page['text'][e['start']:e['end']] != e['quote']:
                raise ValueError('candidate_quote_location')
    source_map = {s.document_id: s for s in sources}
    if len(source_map) != len(sources):
        raise ValueError('duplicate_source')
    split_keys = {'development': set(), 'heldout': set()}
    for case in cases:
        validate_case(case)
        if case['scoring_version'] != manifest['scoring_version']:
            raise ValueError('case_manifest_scoring_version_mismatch')
        row = by_request[case['case_id']]
        if set(row) != {'case_id', 'request', 'document_ids'} or set(row['request']) != REQUEST_FIELDS:
            raise ValueError('request_evaluator_fields')
        request = request_only(row['request'])
        if (request.question != case['question'] or request.company != case['company']
                or request.as_of_date.isoformat() != case['as_of_date']
                or request.risk_categories != [case['risk_category']]):
            raise ValueError('candidate_question_metadata_mismatch')
        for document in row['document_ids']:
            s = source_map[document]
            if s.company != case['company'] or s.company_group != case['company_group']:
                raise ValueError('source_company_mismatch')
            split_keys[case['split']].update([s.company_group, s.source_group_id,
                                            s.raw_sha256, s.parsed_sha256])
        for point in case['obligations']:
            for eid in point['evidence_ids']:
                e = by_evidence[eid]
                if e['document_id'] not in row['document_ids'] or e['published_at'][:10] > case['as_of_date']:
                    raise ValueError('point_source_scope')
    if split_keys['development'] & split_keys['heldout']:
        raise ValueError('company_document_or_copy_split_leakage')
    return CandidateDataset(manifest, by_case, by_request, by_evidence, tuple(sources))
