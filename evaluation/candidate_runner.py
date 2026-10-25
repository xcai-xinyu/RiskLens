"""Candidate preparation path: real workflow functions, explicitly simulated adapters only."""
import asyncio
import copy
import hashlib
import json
import tempfile
from pathlib import Path
from time import monotonic

from app.config import Settings
from app.p1_contracts import AnalystDraft, CorpusEntry
from app.p2_contracts import P2State
from app.p2_workflow import P2Workflow
from app.rag.ingestion import ingest_bytes
from app.rag.retriever import LocalEmbedding, Retriever
from app.storage.repository import Repository
from app.tools.research import ResearchTools
from app.verification import detect_cash_conflicts, hard_checks
from evaluation.candidate_data import RuntimeJob, fingerprint, request_only


def ingest_sources(repository, sources):
    """Only attested catalog entries. No Gold evidence selection, directory glob or review import."""
    provenance = []
    for source in sources:
        source.verify()
        raw = source.raw_path.read_bytes()
        suffix = source.raw_path.suffix
        representation = 'original_bytes'
        if suffix == '.docx':
            # Existing approved parser cache is full-document text, not selected Gold snippets.
            pages = json.loads(source.parsed_path.read_text())
            raw = '\n\n'.join(p['text'] for p in pages).encode()
            suffix = '.txt'
            representation = 'attested_full_document_docx_text'
        entry = CorpusEntry(path=source.raw_path.name, company_id=source.company_group,
                            title=source.title, url=source.url, published_at=source.published_at,
                            source_group_id=source.source_group_id,
                            sha256=hashlib.sha256(raw).hexdigest())
        result = ingest_bytes(repository, raw, suffix, entry)
        if result.status != 'success':
            raise ValueError('candidate_source_ingestion_failed')
        provenance.append({'document_id': source.document_id, 'original_hash': source.raw_sha256,
                           'representation': representation, 'ingested_hash': entry.sha256,
                           'selected_by_gold': False})
    return provenance


class CallBoundary:
    """One total budget/deadline across arms; no SDK/client is constructed here."""
    def __init__(self, adapter, budget, deadline):
        if getattr(adapter, 'is_simulated', False) is not True:
            raise ValueError('unfrozen_candidate_real_calls_forbidden')
        self.adapter, self.budget, self.deadline = adapter, budget, deadline

    def __getattr__(self, name):
        method = getattr(self.adapter, name)
        if name not in {'raw', 'make_plan', 'research_turn', 'extract', 'analyze', 'verify', 'repair'}:
            return method

        async def bounded(*args):
            if self.budget['calls'] >= self.budget['limit']:
                raise ValueError('candidate_call_budget_exhausted')
            left = self.deadline - monotonic()
            if left <= 0:
                raise TimeoutError('candidate_deadline')
            self.budget['calls'] += 1
            self.budget['roles'].append(name)
            return await asyncio.wait_for(method(*args), min(30, left))
        return bounded


async def run_candidate_job(job, *, adapter_factory, embedding, settings=None, include_repair=False):
    if not isinstance(job, RuntimeJob):
        raise TypeError('runtime_job_required_not_case_dict')
    if not isinstance(embedding, LocalEmbedding) and getattr(embedding, 'is_simulated', False) is not True:
        raise ValueError('unfrozen_candidate_real_embedding_forbidden')
    settings = settings or Settings()
    request = request_only(job.request)
    deadline = monotonic() + settings.run_timeout_seconds
    budget = {'calls': 0, 'limit': settings.max_llm_calls, 'roles': []}
    adapters = []

    def adapter():
        wrapped = CallBoundary(adapter_factory(), budget, deadline)
        adapters.append(wrapped)
        return wrapped

    records = []
    # A receives no source objects or repository, and is executed before ingestion.
    try:
        raw = AnalystDraft.model_validate(await adapter().raw(request))
        output = {'status': 'completed', 'claims': [c.model_dump(mode='json') for c in raw.claims],
                  'metrics': [], 'gaps': []}
    except (ValueError, TimeoutError):
        output = {'status': 'failed', 'claims': [], 'metrics': [], 'gaps': ['raw_failed']}
    records.append({'arm': 'A', 'output': output, 'shared_snapshot_hash': None,
                    'runtime_evidence': {}, 'report_context': {'request': request.model_dump(mode='json'), 'output': output}})
    with tempfile.TemporaryDirectory(prefix='risklens-candidate-') as tmp:
        repo = Repository(tmp)
        try:
            provenance = ingest_sources(repo, job.sources)
            retriever = Retriever(repo, Path(tmp) / 'index', embedding)
            await asyncio.wait_for(retriever.rebuild(), max(.001, deadline-monotonic()))
            company_id = job.sources[0].company_group if job.sources else request.company
            tools = ResearchTools(repo, retriever, request, company_id, settings.retrieval_limit)
            metadata = {'label': 'SIMULATED CANDIDATE ENGINEERING CHECK', 'provider': 'injected_simulated',
                        'model': 'injected_simulated', 'source_provenance': provenance}
            workflow = P2Workflow(adapter(), tools, settings, company_id, deadline, metadata)
            state = P2State(run_id=job.case_id, request=request, documents=repo.documents())
            await workflow.planner(state)
            await workflow.research(state)
            await workflow.metrics_node(state)
            await workflow.analyst(state)
            snapshot = {'request': request.model_dump(mode='json'),
                        'evidence': [e.model_dump(mode='json') for e in state.evidence],
                        'metrics': [m.model_dump(mode='json') for m in state.metrics],
                        'draft': [c.model_dump(mode='json') for c in state.draft_claims]}
            shared_hash = fingerprint(snapshot)
            snapshot_hashes = {key: fingerprint(snapshot[key]) for key in ('evidence', 'metrics', 'draft')}
            runtime_evidence = {e.evidence_id: e.model_dump(mode='json') for e in state.evidence}
            base = copy.deepcopy(state)
            c_state = None
            for arm in ('B', 'C', 'D', *(['C_REPAIR'] if include_repair else [])):
                current = copy.deepcopy(base)
                wf = P2Workflow(adapter(), tools, settings, company_id, deadline, metadata)
                if arm == 'B':
                    final = current.draft_claims
                elif arm == 'D':
                    final = [c for c in current.draft_claims if all(hard_checks(
                        c, current.evidence, current.metrics, repo, request.as_of_date)[0].values())
                        and not detect_cash_conflicts(c, current.evidence, repo, request.as_of_date)]
                else:
                    if arm == 'C':
                        await wf.verifier(current)
                        c_state = copy.deepcopy(current)
                    else:
                        # Paired repair starts from C's exact verdict, not a second initial judge call.
                        current = copy.deepcopy(c_state)
                        if wf.after_verifier(current) == 'repair':
                            await wf.repair(current)
                    passed = {v.claim_id for v in current.verification_results if v.status == 'supported'}
                    final = [c for c in current.draft_claims if c.claim_id in passed]
                    await wf.report(current)
                output = {'status': 'failed' if current.errors else ('partial' if current.gaps else 'completed'),
                          'claims': [c.model_dump(mode='json') for c in final],
                          'metrics': snapshot['metrics'],
                          'gaps': [g.model_dump(mode='json') for g in current.gaps]}
                records.append({'arm': arm, 'output': output, 'shared_snapshot_hash': shared_hash,
                                'snapshot_hashes': snapshot_hashes,
                                'runtime_evidence': runtime_evidence,
                                'report_context': current.report.model_dump(mode='json') if current.report else output,
                                'repair_count': current.repair_count})
            return {'case_id': job.case_id, 'simulated': True, 'records': records,
                    'shared_snapshot': snapshot, 'shared_snapshot_hash': shared_hash,
                    'snapshot_hashes': snapshot_hashes,
                    'source_provenance': provenance, 'budget': budget,
                    'upstream_kind': 'PRODUCTION_RESEARCH_METRICS_ANALYST_WITH_SIMULATED_ADAPTER',
                    'gold_used_for_upstream': False, 'real_provider_validation': 'NOT_RUN',
                    'embedding_version': embedding.version, 'cost': None}
        except (ValueError, TimeoutError, RuntimeError, TypeError, KeyError, OSError, IndexError) as error:
            # Preserve completed arms and any generated upstream material before evaluation.
            # Never serialize provider payloads or exception messages into the audit.
            for arm in ('B', 'C', 'D', *(['C_REPAIR'] if include_repair else [])):
                if not any(r['arm'] == arm for r in records):
                    records.append({'arm': arm, 'output': {'status': 'failed', 'claims': [],
                        'metrics': [], 'gaps': ['candidate_runtime_failed']},
                        'runtime_evidence': locals().get('runtime_evidence', {}), 'report_context': {},
                        'shared_snapshot_hash': locals().get('shared_hash'),
                        'error_type': type(error).__name__})
            return {'case_id': job.case_id, 'simulated': True, 'records': records,
                    'shared_snapshot': locals().get('snapshot', {}),
                    'source_provenance': locals().get('provenance', []), 'budget': budget,
                    'error_type': type(error).__name__, 'gold_used_for_upstream': False,
                    'real_provider_validation': 'NOT_RUN', 'cost': None}
        finally:
            repo.close()


async def evaluate_candidate(directory, case_id, *, adapter_factory, embedding,
                             settings=None, reviews=None, include_repair=False, project_root=None,
                             artifact_dir=None, event_sink=None):
    from evaluation.candidate_execution import (
        _write,
        evaluate_sealed,
        load_runtime_input,
        seal_output,
    )
    job, header = load_runtime_input(directory, case_id, project_root=project_root)
    if event_sink:
        event_sink({'event': 'runtime_started', 'case_id': case_id})
    try:
        result = await run_candidate_job(job, adapter_factory=adapter_factory,
            embedding=embedding, settings=settings, include_repair=include_repair)
    except (ValueError, TimeoutError, RuntimeError, TypeError, KeyError, OSError, IndexError) as error:
        # Initialization failure is also an output artifact, never a reason to read Gold first.
        result = {'case_id': case_id, 'simulated': True, 'records': [
            {'arm': arm, 'output': {'status': 'failed', 'claims': [], 'metrics': [], 'gaps': []},
             'runtime_evidence': {}, 'report_context': {}, 'shared_snapshot_hash': None,
             'error_type': type(error).__name__}
            for arm in ('A', 'B', 'C', 'D', *(['C_REPAIR'] if include_repair else []))],
            'gold_used_for_upstream': False, 'real_provider_validation': 'NOT_RUN', 'cost': None}
    sealed = seal_output(result, header, artifact_dir=artifact_dir, event_sink=event_sink)
    try:
        evaluated = evaluate_sealed(sealed, project_root=project_root, reviews=reviews, event_sink=event_sink)
    except (ValueError, TimeoutError, RuntimeError, TypeError, KeyError, OSError, IndexError) as error:
        _write(sealed / 'evaluation-error.json', {'status': 'EVALUATION_REJECTED',
                                                'error_type': type(error).__name__})
        raise
    _write(sealed / 'evaluated.json', evaluated)
    return evaluated
