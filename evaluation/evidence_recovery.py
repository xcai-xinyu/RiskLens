"""Attested local Research closure and scoped continuation of attempt-046."""
import copy
import json
from decimal import Decimal
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from app.p1_contracts import ToolEvent
from app.p2_contracts import P2State
from app.schemas import Evidence
from evaluation.batch_continuation import ContinuationLedger, replay
from evaluation.candidate_data import file_hash, fingerprint
from evaluation.context_protocol import unique_evidence
from evaluation.context_runtime import ContextWorkflow
from evaluation.evidence_policy import VERSION, EvidenceExecution
from evaluation.frozen_authorization import PaidPermit, validate_manifest
from evaluation.frozen_batch import validate_seal
from evaluation.frozen_budget import Ledger
from evaluation.frozen_index import source_identity
from evaluation.openai_runtime import OpenAIAccount
from evaluation.research_reuse import ResearchReuse
from evaluation.ten_batch_budget import TenLimits

RECOVER = 'v3-heldout-14'
REMAINING = [RECOVER, 'v3-heldout-17', 'v3-heldout-19']
CHANGED_EXISTING = {'evaluation/context_runtime.py', 'evaluation/openai_runtime.py',
                    'evaluation/ten_batch_runner.py'}


def restore_finished_research(directory, job, old_manifest, new_manifest, repository, execution):
    """Read only sealed runtime files; successful provider finish is never regenerated."""
    directory = Path(directory)
    for line in (directory.parent/'SHA256SUMS').read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        if Path(name).is_absolute() or '..' in Path(name).parts or file_hash(directory.parent/name) != digest:
            raise ValueError('research_checkpoint_tampered')
    validate_seal(directory/job.case_id)
    if job.case_id != RECOVER or any(old_manifest[k] != new_manifest[k]
            for k in ('providers', 'frozen_directory', 'freeze_sha256')):
        raise ValueError('research_checkpoint_configuration')
    if any(new_manifest['runtime_files'].get(p) != h for p, h in old_manifest['runtime_files'].items()
           if p not in CHANGED_EXISTING):
        raise ValueError('research_upstream_code_drift')
    old_cfg, new_cfg = copy.deepcopy(old_manifest['context']), copy.deepcopy(new_manifest['context'])
    new_cfg.pop('evidence_policy')
    new_cfg['max_retained_evidence'] = old_cfg['max_retained_evidence']
    if old_cfg != new_cfg:
        raise ValueError('research_context_drift')
    path = directory/job.case_id/'runtime-output.json'
    result = json.loads(path.read_text())['result']
    if (result.get('error') != 'CONTEXT_EVIDENCE_COUNT_BLOCKED' or result.get('failed_arm') != 'B'
            or result.get('shared_snapshot')):
        raise ValueError('research_checkpoint_wrong_stage')
    requests = [r for r in result['context_execution']['requests'] if r['role'] == 'research']
    if len(requests) != 5 or any(r['status'] != 'COMPLETED' for r in requests):
        raise ValueError('research_not_completed')
    events, call_ids = [], []
    for request in requests:
        wire = request['wire_request']
        calls = {v['call_id']: v for v in wire['input'] if v.get('type') == 'function_call'}
        for item in wire['input']:
            if item.get('type') != 'function_call_output':
                continue
            cid = item['call_id']
            if cid in call_ids or cid not in calls or calls[cid]['name'] != 'search_documents':
                raise ValueError('checkpoint_tool_link_invalid')
            call_ids.append(cid)
            events.append(ToolEvent(name='search_documents', result=json.loads(item['output'])))
    last = requests[-1]['provider_output']
    output = last['output']
    if (last['status'] != 'completed' or any(v.get('type') == 'function_call' for v in output)
            or not any(v.get('type') == 'message' and v.get('status') == 'completed'
                       for v in output) or len(events) != 4):
        raise ValueError('research_finish_not_attested')
    state = P2State.model_validate_json((directory/(job.case_id+'-stages')/'planner.json').read_text())
    if state.request != job.request or state.run_id != job.case_id or state.errors:
        raise ValueError('research_checkpoint_identity')
    state.tool_events = events
    state.budget.tool_calls = len(events)
    state.budget.llm_calls = len(result['context_execution']['requests'])
    state.documents = repository.documents()
    state.document_ids = [d.document_id for d in state.documents]
    model = SimpleNamespace(execution=execution, ledger=execution.ledger, coverage={})
    workflow = ContextWorkflow(model, SimpleNamespace(repository=repository), None, None, None, {})
    workflow.finalize_research(state)
    coverage = result['context_execution']['coverage']['research_lossless']
    if (set(coverage['evidence_ids']) != {e.evidence_id for e in state.evidence}
            or len(state.evidence) != 33):
        raise ValueError('research_union_changed')
    # Byte-identical original Evidence values, not reconstructed/summarized quotes.
    original = unique_evidence([Evidence.model_validate(e)
                               for event in events for e in event.result.data])
    if state.evidence != original:
        raise ValueError('research_evidence_changed')
    return ResearchReuse(state.model_dump(mode='json'), source_identity(job.sources), {
        'source': str(path), 'sha256': file_hash(path),
        'source_seal': file_hash(directory/job.case_id/'OUTPUT_SEAL.json'),
        'provider_response_id': last.get('response_id'), 'tool_call_ids': call_ids,
        'local_finalizer': VERSION, 'evidence_count': len(state.evidence),
        'evidence_hash': fingerprint([e.model_dump(mode='json') for e in state.evidence]),
        'new_research_calls': 0, 'new_query_embedding_calls': 0,
        'boundary': 'Successful Research finish replayed locally; original 33 evidence and four results preserved'})


class EvidenceLedger(ContinuationLedger):
    def case_limit(self, kind):
        if self.case_id == RECOVER and kind == 'chat':
            return self.limits.case_chat_calls + 6
        return super().case_limit(kind)

    @property
    def case_seconds_limit(self):
        return self.limits.case_seconds + (600 if self.case_id == RECOVER else 0)

    def begin_case(self, case_id):
        Ledger.begin_case(self, case_id)
        self.case_counts = dict(self.prior_case_counts.get(case_id, {'chat': 0, 'embedding': 0}))
        elapsed = self.prior_elapsed.get(case_id, 0)
        self.deadline = monotonic() + self.case_seconds_limit - elapsed
        self.append('case_prior_consumption', case_id=case_id, counts=dict(self.case_counts),
                    prior_elapsed_seconds=elapsed, cumulative_seconds_cap=self.case_seconds_limit,
                    effective_chat_cap=self.case_limit('chat'), allowance_reset=False,
                    additional_chat=6 if case_id == RECOVER else 0,
                    additional_seconds=600 if case_id == RECOVER else 0, logical_queries_added=0)
        self.check()


def authorize_remaining(manifest_path, authorization_path, root, *, claim_allocation=True):
    root = Path(root)
    manifest = validate_manifest(manifest_path, root)
    auth = json.loads(Path(authorization_path).read_text())
    old = root/auth['prior_attempt']
    original = root/'evaluation/audit/P4/attempt-040'
    allocation_auth = json.loads((original/'AUTHORIZATION.json').read_text())
    allocations = [json.loads(x) for x in (original/'project-ledger/allocations.jsonl').read_text().splitlines()]
    old_manifest = json.loads((old/'RUN_MANIFEST.json').read_text())
    selection = json.loads((root/manifest['selection_path']).read_text())
    ids = [r['case_id'] for r in selection['selected']]
    limits = TenLimits.model_validate(manifest['limits']['heldout'])
    account = OpenAIAccount.model_validate(auth['account_limits'])
    expected = EvidenceExecution(SimpleNamespace(limits=limits), account).configuration()
    status = json.loads((old/'heldout/EXECUTION_STATUS.json').read_text())
    if (auth['status'] != 'APPROVED' or not auth['approval_source'] or
            auth['version'] != VERSION or auth['usd_cap'] != '8' or auth['automatic_retry'] != 0 or
            auth['run_manifest_sha256'] != file_hash(manifest_path) or
            auth['case_ids'] != REMAINING or manifest['batches']['heldout'] != REMAINING or
            ids != allocation_auth['case_ids'] or ids[7:] != REMAINING or
            [r['case_id'] for r in status['rows'][:7]] != ids[:7] or
            any(r['error'] or r['shared_hash_validation'] != 'PASS' for r in status['rows'][:7]) or
            manifest['providers'] != old_manifest['providers'] or
            manifest['freeze_sha256'] != old_manifest['freeze_sha256'] or
            manifest['context'] != expected or manifest['readiness']['heldout'] != 'PASS' or
            limits.model_dump(mode='json') != allocation_auth['limits'] or len(allocations) != 1 or
            allocations[0]['authorization_sha256'] != file_hash(original/'AUTHORIZATION.json') or
            allocations[0]['allocation_id'] != fingerprint(allocation_auth) or
            allocations[0]['usd_cap'] != '8' or allocations[0]['project_usd_cap'] != '8' or
            auth['additional_chat'] != 6 or auth['additional_seconds'] != 600):
        raise ValueError('evidence_recovery_authorization_mismatch')
    prior = replay(old/'heldout/budget_ledger.jsonl', limits,
        ancestors=tuple(root/p for p in auth['ancestor_ledgers']), isolated_requests=auth['isolated_requests'])
    events = [json.loads(x) for x in (old/'heldout/budget_ledger.jsonl').read_text().splitlines()]
    result = json.loads((old/'heldout'/RECOVER/'runtime-output.json').read_text())['result']
    if (prior['source_sha256'] != auth['prior_ledger_sha256'] or
            Decimal(prior.get('unknown_holds_usd', '0')) != Decimal('0.19309250') or
            [r['code'] for r in events if r['event'] == 'halted'] != ['CONTEXT_EVIDENCE_COUNT_BLOCKED'] or
            auth['elapsed_by_case'][RECOVER] != result['elapsed_seconds'] or
            any(cid in prior['case_counts'] for cid in REMAINING[1:])):
        raise ValueError('evidence_recovery_provenance_invalid')
    if claim_allocation:
        guards = root/'data/runtime-cache/batch-continuations'
        guards.mkdir(parents=True, exist_ok=True)
        with (guards/(prior['prior_hash']+'.json')).open('x') as stream:
            json.dump({'authorization_sha256': file_hash(authorization_path),
                       'allocation_id': prior['allocation_id'], 'previous_ledger_hash': prior['prior_hash']}, stream)
            stream.flush()
            import os
            os.fsync(stream.fileno())
    return manifest, auth, limits, account, PaidPermit(tuple(REMAINING), prior['allocation_id'], fingerprint(expected))


def continue_isolated_case(cid, result, ledger, context, adapter, embedding):
    """Only identified case-local failures after clients close; never integrity/auth/budget."""
    allowed = {'provider_timeout', 'workflow_metric_extraction_error', 'workflow_analyst_contract_error',
               'context_incomplete_output', 'model_parse_error', 'provider_schema_error',
               'provider_json_parse_failed', 'provider_schema_validation_failed',
               'provider_incomplete_output', 'provider_empty_output', 'provider_refusal',
               'semantic_verification_failed', 'CONTEXT_CALL_BUDGET_BLOCKED',
               'call_limit', 'case_timeout'}
    error = result.get('error')
    if (error not in allowed or ledger.halted not in (None, error) or ledger.pending or
            not adapter.client.is_closed() or not embedding.client.is_closed() or
            context.quota.next_blocker or context.quota.account.known_blocking_error):
        return False
    if any(ledger.counts[k] >= getattr(ledger.limits, k + '_calls') for k in ('chat', 'embedding')):
        return False
    ended = [r for r in ledger.events if r['event'] == 'request_local_task_finished']
    if error == 'provider_timeout' and (not ended or not ended[-1]['task_done']):
        return False
    ledger.append('isolated_case_failure', case_id=cid, error=error, unknown_hold_retained=True,
                  clients_closed=True, case_not_retried=True)
    ledger.halted = None
    ledger.case_id = None
    context.quota.pending = None
    return True
