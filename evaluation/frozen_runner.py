"""Opt-in A/B/D/C execution of the frozen protocol; no evaluator or Gold loader."""
import copy
from pathlib import Path
from time import monotonic

from app.config import Settings
from app.p1_contracts import AnalystDraft
from app.p2_contracts import P2State
from app.p2_workflow import P2Workflow
from app.storage.repository import Repository
from app.tools.research import ResearchTools
from app.verification import detect_cash_conflicts, hard_checks
from evaluation.candidate_data import RuntimeJob, fingerprint, request_only
from evaluation.candidate_execution import _write
from evaluation.frozen_adapters import BudgetedEmbedding, BudgetedModel, safe_error
from evaluation.frozen_budget import BudgetStop
from evaluation.frozen_index import prepare_index, source_identity

ARMS = ('A', 'B', 'D', 'C')


def snapshot_hashes(snapshot):
    return {key: fingerprint(snapshot[key]) for key in ('sources', 'evidence', 'metrics', 'draft')}


def named_hashes(hashes):
    return {'source_snapshot_hash': hashes['sources'], 'evidence_snapshot_hash': hashes['evidence'],
            'metric_snapshot_hash': hashes['metrics'], 'baseline_draft_hash': hashes['draft']}


def validate_shared(result):
    if not result.get('shared_snapshot'):
        return 'UNAVAILABLE_UPSTREAM_FAILURE'
    hashes = snapshot_hashes(result['shared_snapshot'])
    whole = fingerprint(result['shared_snapshot'])
    for record in result['records']:
        if record['arm'] != 'A' and (record.get('snapshot_hashes') != hashes or
                                    record.get('shared_snapshot_hash') != whole or
                                    any(record.get(k) != v for k, v in named_hashes(hashes).items())):
            raise ValueError('COMPARISON_INVALID')
    return 'PASS'


class FrozenTools(ResearchTools):
    """Same authorized local tool implementation, with fail-stop experiment policy."""
    def __init__(self, *args, ledger, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = ledger

    async def execute(self, call):
        self.ledger.check()
        if call.name != 'search_documents':
            self.ledger.stop('network_or_other_tool_forbidden')
        result = await super().execute(call)
        self.ledger.check()
        if result.status != 'success':
            self.ledger.stop('retrieval_' + result.status)
        return result


async def run_frozen_job(job, *, adapter, embedding, ledger, directory, index_directory,
                         paid_permit=None, context=None):
    if not isinstance(job, RuntimeJob) or job.request.mode != 'offline_research':
        raise ValueError('frozen_offline_runtime_job_required')
    simulated = getattr(adapter, 'is_simulated', False) is True
    if simulated != (getattr(embedding, 'is_simulated', False) is True):
        raise ValueError('mixed_simulation_forbidden')
    if not simulated and (paid_permit is None or not paid_permit.allows(job.case_id, ledger)):
        raise PermissionError('BLOCKED_BUDGET_APPROVAL')
    if not simulated and getattr(paid_permit, 'context_hash', None) and context is None:
        raise PermissionError('BLOCKED_CONTEXT_AUTHORIZATION')
    if context is not None:
        from evaluation.context_budget import ContextLimits, validate_account
        if not isinstance(ledger.limits, ContextLimits) or context.ledger is not ledger:
            raise ValueError('context_configuration_mismatch')
        if not simulated:
            if getattr(paid_permit, 'context_hash', None) != fingerprint(context.configuration()):
                raise PermissionError('BLOCKED_CONTEXT_AUTHORIZATION')
            validate_account(context.quota.account, ledger.limits)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    started = monotonic()
    ledger.begin_case(job.case_id)
    request = request_only(job.request)
    model = context.model_wrapper(adapter) if context else BudgetedModel(adapter, ledger)
    vectors = context.embedding_wrapper(embedding) if context else BudgetedEmbedding(embedding, ledger)
    settings = Settings(max_llm_calls=ledger.case_limit("chat"),
                        run_timeout_seconds=ledger.case_seconds_limit)
    result = {'case_id': job.case_id, 'simulated': simulated, 'records': [],
              'mode': 'simulated-only' if simulated else 'fixed-corpus-real-provider',
              'shared_snapshot': {}, 'source_provenance': [], 'gold_used_for_upstream': False,
              'provider': adapter.provider, 'model': adapter.model,
              'embedding_version': embedding.version, 'cost': None,
              'upstream_kind': 'PRODUCTION_RESEARCH_METRICS_ANALYST',
              'real_provider_validation': 'NOT_RUN' if simulated else 'ATTEMPTED'}
    state, repo = None, None
    active_arm = 'A'

    def record(arm, output, **extra):
        row = {'arm': arm, 'output': output, 'runtime_evidence': {},
               'shared_snapshot_hash': None, 'report_context': output, 'repair_count': 0,
               **extra}
        row['usage_records'] = [r for r in ledger.events if r['event'] == 'settled'
                               and r['arm'] == arm and r['case_id'] == job.case_id]
        _write(directory / (arm + '.json'), row)
        result['records'].append(row)

    def output(claims, status='completed'):
        if status == 'completed' and state and state.gaps:
            status = 'partial'
        return {'status': status, 'claims': [c.model_dump(mode='json') for c in claims],
                'metrics': [m.model_dump(mode='json') for m in state.metrics] if state else [],
                'gaps': [g.model_dump(mode='json') for g in state.gaps] if state else []}

    try:
        scoped_cache = (context is not None and
                        getattr(context.quota.account, 'admission_version', None) in
                        {'s3-bounded-quota-v2', 'openai-responses-context-v1'})
        if scoped_cache:
            from evaluation.s3_preflight import admit_plan, index_plan
            if context.quota.account.admission_version == 'openai-responses-context-v1':
                from evaluation.openai_runtime import admit_plan
            plan = index_plan(job, vectors, directory / 'budget-preflight-repository', index_directory)
            checkpoint = getattr(context, 'research_reuse', None)
            if checkpoint is not None:
                from evaluation.research_reuse import ResearchReuse
                if not isinstance(checkpoint, ResearchReuse):
                    raise ValueError('invalid_research_reuse')
                plan = checkpoint.remaining_plan(job, plan)
            admit_plan = getattr(context, 'admit_plan', admit_plan)
            plan = admit_plan(plan, ledger.limits, context.quota.account)
            _write(directory / 'WHOLE_CASE_PREFLIGHT.json', plan)
            ledger.append('whole_case_budget_admitted_before_A', **plan)
        # A receives only the request. Optional whole-case capacity inspection above
        # exposes no corpus to its adapter and dispatches no embedding calls.
        raw = AnalystDraft.model_validate(await model.raw(request))
        prior = getattr(context, 'baseline_reuse', None)
        record('A', output(raw.claims), **({'execution_status': 'reused',
               'reuse': prior.provenance, 'newly_generated': False} if prior else {}))
        active_arm = model.arm = 'B'
        repo = Repository(directory / 'repository')
        retriever, provenance, index = await prepare_index(repo, job.sources, vectors,
                                                          index_directory, request,
                                                          scoped_cache=scoped_cache)
        result.update(source_provenance=provenance, index=index)
        ledger.check()
        tools = FrozenTools(repo, retriever, request, job.sources[0].company_group,
                            settings.retrieval_limit, ledger=ledger)
        factory = context.workflow if context else P2Workflow
        workflow = factory(model, tools, settings, job.sources[0].company_group,
                           ledger.deadline, {'mode': result['mode']})
        state = P2State(run_id=job.case_id, request=request, documents=repo.documents())
        stages = ('planner', 'research', 'metrics_node', 'analyst')
        checkpoint = getattr(context, 'research_reuse', None)
        if checkpoint is not None:
            from evaluation.research_reuse import ResearchReuse
            if not isinstance(checkpoint, ResearchReuse):
                raise ValueError('invalid_research_reuse')
            state = checkpoint.materialize(job, repo)
            model.coverage['retained_evidence_ids'] = [e.evidence_id for e in state.evidence]
            result['research_reuse'] = checkpoint.provenance
            ledger.append('research_reused', provenance=checkpoint.provenance)
            _write(directory / 'research.json', state.model_dump(mode='json'))
            stages = ('metrics_node', 'analyst')
        resume = getattr(context, 'research_resume', None)
        if resume is not None:
            from evaluation.research_checkpoint import ResearchResume
            if checkpoint is not None or not isinstance(resume, ResearchResume):
                raise ValueError('invalid_partial_research_resume')
            state = resume.materialize(job, repo)
            state.budget.llm_calls = ledger.case_counts['chat']
            result['research_resume'] = resume.provenance
            ledger.append('research_checkpoint_resumed', provenance=resume.provenance)
            (directory / 'resume').mkdir(exist_ok=False)
            _write(directory / 'resume' / 'research.json', state.model_dump(mode='json'))
            stages = ('research', 'metrics_node', 'analyst')
        for stage in stages:
            await getattr(workflow, stage)(state)
            _write(directory / (stage + '.json'), state.model_dump(mode='json'))
            ledger.check()
            if state.errors:
                ledger.stop('workflow_' + state.errors[-1].code)
            if stage == 'research' and not state.evidence:
                ledger.stop('no_retrieved_evidence')
        snapshot = {'sources': source_identity(job.sources),
                    'evidence': [e.model_dump(mode='json') for e in state.evidence],
                    'metrics': [m.model_dump(mode='json') for m in state.metrics],
                    'draft': [c.model_dump(mode='json') for c in state.draft_claims]}
        hashes = snapshot_hashes(snapshot)
        shared = {'snapshot_hashes': hashes, 'shared_snapshot_hash': fingerprint(snapshot),
                  'runtime_evidence': {e.evidence_id: e.model_dump(mode='json') for e in state.evidence},
                  **named_hashes(hashes)}
        result.update(shared_snapshot=snapshot, snapshot_hashes=hashes,
                      shared_snapshot_hash=fingerprint(snapshot))
        _write(directory / 'B_INITIAL_SNAPSHOT.json', snapshot)
        record('B', output(state.draft_claims), **shared)
        active_arm = 'D'
        check_claim = getattr(workflow, 'hard_checks', hard_checks)
        checks = {c.claim_id: {'checks': check_claim(c, state.evidence, state.metrics, repo,
                  request.as_of_date)[0], 'conflicts': detect_cash_conflicts(c, state.evidence,
                  repo, request.as_of_date)} for c in state.draft_claims}
        passed = {cid for cid, v in checks.items() if all(v['checks'].values()) and not v['conflicts']}
        record('D', output([c for c in state.draft_claims if c.claim_id in passed]),
               deterministic_checks=checks, **shared)
        active_arm = model.arm = 'C'
        c_state = copy.deepcopy(state)
        # Reuse P2 verification. It computes the same hard/conflict checks, then semantic
        # checks. Existing adjudication enforces conflicts even if the LLM says supported.
        await workflow.verifier(c_state)
        _write(directory / 'verifier.json', c_state.model_dump(mode='json'))
        ledger.check()
        if any(g.code in {'semantic_unavailable', 'run_timeout'} for g in c_state.gaps):
            ledger.stop('semantic_verification_failed')
        accepted = {v.claim_id for v in c_state.verification_results if v.status == 'supported'}
        record('C', output([c for c in state.draft_claims if c.claim_id in accepted]),
               deterministic_checks=checks,
               verification_results=[v.model_dump(mode='json') for v in c_state.verification_results],
               **shared)
        result['shared_hash_validation'] = validate_shared(result)
    except Exception as exc:  # noqa: BLE001 - retain partial artifacts, redact external errors
        reason = safe_error(exc)
        result.update(error=reason, failed_arm=active_arm)
        if not ledger.halted:
            try:
                ledger.stop(reason)
            except BudgetStop:
                pass
        for arm in ARMS:
            if not any(r['arm'] == arm for r in result['records']):
                shared = ({'snapshot_hashes': result['snapshot_hashes'],
                           'shared_snapshot_hash': result['shared_snapshot_hash'],
                           **named_hashes(result['snapshot_hashes']),
                           'runtime_evidence': {e.evidence_id: e.model_dump(mode='json')
                                                for e in state.evidence}}
                          if result.get('shared_snapshot') else {})
                record(arm, output([], 'failed'), error=reason,
                       execution_status='failed' if arm == active_arm else 'not_started', **shared)
    finally:
        if repo:
            repo.close()
    result.update(budget=ledger.summary(), elapsed_seconds=monotonic() - started)
    if context:
        result['context_execution'] = context.report()
    _write(directory / 'result.json', result)
    return result
