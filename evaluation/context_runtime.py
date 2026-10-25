"""Lossless context-v1 coordination through the existing P2 workflow boundary."""
import copy
import hashlib
import re
from time import monotonic

from app.p1_contracts import AnalystDraft
from app.p2_contracts import MetricExtraction, SemanticBatch
from app.p2_workflow_v2 import P2Workflow
from app.schemas import Evidence, Gap
from app.tools.metrics import FORMULAS
from evaluation.candidate_data import fingerprint, request_only
from evaluation.context_budget import QuotaGate
from evaluation.context_protocol import (
    CAPS,
    PROMPT_VERSION,
    VERSION,
    TokenCounter,
    pack_all,
    packet_record,
    unique_evidence,
)
from evaluation.frozen_adapters import paid_call
from evaluation.frozen_budget import BudgetStop


def configuration(limits, account):
    if getattr(account, 'admission_version', None) == 'openai-responses-context-v1':
        from evaluation.openai_runtime import configuration as openai_configuration
        return openai_configuration(limits, account)
    result = {'version': VERSION, 'account': account.model_dump(mode='json'),
            'limits': limits.model_dump(mode='json'),
            'prompt_version': PROMPT_VERSION, 'output_caps': CAPS,
            'tokenizer': 'o200k_harmony-local-v1',
            'input_margin': 'max(512,ceil(serialized_components*0.10))',
            'max_input_plus_output': 7600, 'chat_response_spacing_seconds': 65,
            'account_admission_max_snapshot_age_seconds': 300,
            'retrieval_candidates_per_search': 12, 'max_retained_evidence': 32,
            'max_searches': 4, 'packing': 'complete-chunks-all-batches-no-ranking-v1',
            'verifier': 'cited-support-plus-all-claims-all-evidence-sweep-v1',
            'analyst': 'complete-batches-union-no-draft-deletion-v1'}
    if getattr(account, 'admission_version', None):
        result['admission_version'] = account.admission_version
        result['chat_response_spacing_seconds'] = None
        result['quota_pacing'] = 'observed-capacity-or-stop-no-automatic-sleep'
    if getattr(account, 'admission_version', None) == 's3-bounded-quota-v2':
        result['quota_pacing'] = 'bounded-local-rolling-and-server-reset-unknown-authorized'
        result['account_admission_max_snapshot_age_seconds'] = None
        result['cost_basis'] = 'conservative-list-price-equivalent-including-free-chat'
        result['whole_case_preflight'] = 'complete-corpus-plus-four-queries-before-A-v1'
        result['vector_cache_scope'] = 'exact-corpus-config-hash'
    return result


class ContextExecution:
    """Explicit opt-in; caller must bind this version/account in its run manifest."""
    def __init__(self, ledger, account, *, counter=None, clock=monotonic, sleep=None):
        self.ledger = ledger
        self.counter = counter or TokenCounter()
        options = {'clock': clock}
        if sleep is not None:
            options['sleep'] = sleep
        gate = QuotaGate
        if getattr(account, 'admission_version', None):
            from evaluation.cold_start import ColdStartQuota
            gate = ColdStartQuota
        if getattr(account, 'admission_version', None) == 's3-bounded-quota-v2':
            from evaluation.s3_quota import S3Quota
            gate = S3Quota
        self.quota = gate(ledger, account, **options)
        self.model = None

    def configuration(self):
        return configuration(self.ledger.limits, self.quota.account)

    def model_wrapper(self, adapter):
        self.model = ContextModel(adapter, self)
        return self.model

    def embedding_wrapper(self, adapter):
        return ContextEmbedding(adapter, self)

    def workflow(self, model, tools, settings, company_id, deadline, metadata):
        # Allow the finish turn after the fourth search; refuse a fifth tool request
        # in research_turn before executing it (legacy loop checks tool budget first).
        settings.max_tool_calls = 5
        model.repository = tools.repository
        return ContextWorkflow(model, tools, settings, company_id, deadline, metadata)

    def report(self):
        return {'configuration': self.configuration(),
                'configuration_hash': fingerprint(self.configuration()),
                'requests': self.model.records if self.model else [],
                'batch_outputs': self.model.outputs if self.model else [],
                'coverage': self.model.coverage if self.model else {},
                'real_tokenizer_error': 'NOT_VERIFIED_WITH_LIVE_PROVIDER',
                'gold_used': False}


class ContextEmbedding:
    def __init__(self, adapter, execution):
        self.adapter, self.execution = adapter, execution
        self.version, self.dimensions = adapter.version, adapter.dimensions
        self.purpose = 'index'

    async def embed(self, texts):
        if not texts or len(texts) > 32 or any(not t or len(t.encode()) > 8192 for t in texts):
            self.execution.ledger.stop('BLOCKED_CORPUS_INDEX_BUDGET')
        inputs = sum(len(t.encode()) for t in texts)
        validator = getattr(self.execution.quota, 'validate_embedding', None)
        if validator is not None:
            validator(texts, self.adapter)
        await self.execution.quota.admit('embedding', inputs)
        arm = getattr(self.execution.quota, 'arm', 'B')
        result = await paid_call(self.adapter, self.execution.ledger, kind='embedding', arm=arm,
            role=self.purpose, inputs=inputs, outputs=0, method=self.adapter.embed, args=(texts,))
        observer = getattr(self.execution.quota, 'observe_embedding', None)
        if observer is not None:
            observer(getattr(self.adapter, 'last_response_metadata', None))
        self.execution.quota.finish('embedding', self.adapter.usage[-1])
        return result


class ContextModel:
    def __init__(self, adapter, execution):
        self.adapter, self.execution = adapter, execution
        self.ledger, self.counter = execution.ledger, execution.counter
        self.arm, self.repository = 'A', None
        self.records, self.outputs, self.coverage = [], [], {}
        self.all_claims, self.metric_conflicts = [], []
        self.request = None

    def __getattr__(self, key):
        if key == 'repair':
            raise BudgetStop('repair_forbidden')
        return getattr(self.adapter, key)

    def prepare(self, role, data):
        return self.adapter.prepare(role, data, self.counter)

    def fits(self, packet):
        if hasattr(self.execution, 'capacity'):
            return self.execution.capacity(packet)
        b = packet.breakdown
        return (b['estimated_input'] <= self.ledger.limits.chat_input_tokens and
                b['output_reservation'] <= self.ledger.limits.chat_output_tokens and
                b['total_estimated'] <= min(7600, self.execution.quota.account.chat.tpm))

    def require_calls(self, count):
        if self.ledger.case_counts['chat'] + count > self.ledger.case_limit('chat'):
            self.ledger.stop('CONTEXT_CALL_BUDGET_BLOCKED')

    async def dispatch(self, packet):
        row = dict(packet_record(packet), arm=self.arm)
        self.records.append(row)
        if (packet.breakdown != self.counter.breakdown(packet.wire,
                packet.wire.get('max_output_tokens', packet.wire.get('max_completion_tokens'))) or not self.fits(packet)):
            row['status'] = 'CONTEXT_BUDGET_BLOCKED'
            self.ledger.stop('CONTEXT_BUDGET_BLOCKED')
        self.require_calls(1)
        if getattr(self.execution.quota.account, 'admission_version', None):
            row['wire_request'] = (self.adapter.audit_wire(packet.wire) if hasattr(self.adapter, 'audit_wire')
                                   else copy.deepcopy(packet.wire))
            if hasattr(self.adapter, 'validate_packet'):
                self.adapter.validate_packet(packet)
            elif (self.adapter.provider != 'groq' or self.adapter.model != 'openai/gpt-oss-120b'
                    or packet.wire['model'] != self.adapter.model
                    or packet.wire['temperature'] != 0 or packet.wire['seed'] != 42):
                self.ledger.stop('provider_configuration_drift')
            if not hasattr(self.adapter, 'validate_packet') and not getattr(self.adapter, 'is_simulated', False):
                client = self.adapter.client
                if (client.openai_api_base.rstrip('/') != 'https://api.groq.com/openai/v1' or
                        str(client.root_async_client.base_url).rstrip('/') != client.openai_api_base.rstrip('/')
                        or client.model_name != self.adapter.model or client.max_retries != 0
                        or client.root_async_client.max_retries != 0):
                    self.ledger.stop('provider_configuration_drift')
        b = packet.breakdown
        from evaluation.frozen_adapters import safe_error
        before_calls = self.adapter.calls
        try:
            await self.execution.quota.admit('chat', b['estimated_input'], b['output_reservation'])
            row['status'] = 'DISPATCHED'
            result = await paid_call(self.adapter, self.ledger, kind='chat', arm=self.arm,
                role=packet.role, inputs=b['estimated_input'], outputs=b['output_reservation'],
                method=self.adapter.send, args=(packet,))
        except Exception as exc:
            row.update(status='FAILED', error=safe_error(exc))
            raise
        finally:
            if self.adapter.calls > before_calls:
                row['provider_metadata'] = getattr(self.adapter, 'last_response_metadata', None)
                row['provider_output'] = getattr(self.adapter, 'last_output', None)
                row['usage'] = self.adapter.usage[-1] if self.adapter.usage else None
        usage = self.adapter.usage[-1]
        row.update(status='COMPLETED', usage=usage,
                   input_estimate_minus_usage=b['estimated_input'] - usage['input_tokens'],
                   usage_is_simulated=getattr(self.adapter, 'is_simulated', False))
        self.outputs.append({'role': packet.role, 'payload_hash': fingerprint(packet.wire),
                             'result': result.model_dump(mode='json')})
        self.execution.quota.finish('chat', usage, getattr(self.adapter, 'last_quota_headers', {}))
        return result

    def evidence_rows(self, evidence):
        rows = []
        for e in unique_evidence(evidence):
            row = e.model_dump(mode='json')
            if self.repository:
                trace = self.repository.trace(e, self.request.as_of_date)
                d = trace['document']
                row.update(source_title=d['title'], published_at=d['published_at'],
                           source_hash=d['content_hash'])
            row['quote_sha256'] = hashlib.sha256(e.quote.encode()).hexdigest()
            rows.append(row)
        return rows

    async def raw(self, request):
        self.request = request_only(request)
        packet = self.prepare('raw', self.request.model_dump(mode='json'))
        prior = getattr(self.execution, 'baseline_reuse', None)
        if prior is not None:
            from evaluation.baseline_reuse import BaselineReuse
            if not isinstance(prior, BaselineReuse):
                raise ValueError('invalid_baseline_reuse')
            result = prior.materialize(packet)
            self.records.append(dict(packet_record(packet), arm='A', status='REUSED',
                                     reuse=prior.provenance, newly_generated=False))
            self.ledger.append('baseline_reused', provenance=prior.provenance)
            return result
        return await self.dispatch(packet)

    async def make_plan(self, request, company_id):
        self.request = request_only(request)
        return await self.dispatch(self.prepare('planner', {
            'request': self.request.model_dump(mode='json'), 'company_id': company_id}))

    async def research_turn(self, context):
        # Explicit projection: old full tool results remain in State/Repository.
        # The Research node gets only discovery metadata, never a shortened quote.
        evidence, history = [], []
        for event in context['tool_events']:
            raw = event['result'].get('data') or []
            evidence.extend(Evidence.model_validate(e) for e in raw)
            history.append({'tool': event['name'], 'status': event['result']['status'],
                            'evidence_ids': [e['evidence_id'] for e in raw]})
        catalogue = []
        for row in self.evidence_rows(evidence):
            catalogue.append({k: v for k, v in row.items() if k not in ('quote', 'relevance')})
        data = {'request': request_only(context['request']).model_dump(mode='json'),
                'plan': context['plan'], 'company_id': context['company_id'],
                'tool_history': history, 'catalogue': catalogue, 'budget': context['budget'],
                'representation': 'discovery_catalogue_only_complete_quotes_retained_downstream'}
        if hasattr(self.adapter, 'validate_packet') and context['tool_events']:
            # Responses function_call_output uses the real, complete latest result.
            data['_tool_result'] = context['tool_events'][-1]['result']
        result = await self.dispatch(self.prepare('research', data))
        if context['budget']['tool_calls'] >= 4 and result.action == 'tool':
            self.ledger.stop('CONTEXT_SEARCH_BUDGET_BLOCKED')
        return result

    async def extract(self, request, evidence, required_metrics):
        if any(name not in FORMULAS for name in required_metrics):
            raise ValueError('unregistered_formula')
        # Numeral-bearing chunks are input candidates, not preselected Gold inputs.
        # Non-numeric qualitative context still goes to ALL Analyst/consistency batches.
        candidates = [e for e in unique_evidence(evidence) if re.search(r'\d', e.quote)]
        rows = self.evidence_rows(candidates)
        packets = pack_all(rows, lambda batch: self.prepare('extract', {
            'request': request_only(request).model_dump(mode='json'),
            'required_metrics': required_metrics, 'evidence': batch}), self.fits)
        self.require_calls(len(packets))
        self.coverage['metric_candidate_evidence_ids'] = [e.evidence_id for e in candidates]
        self.coverage['metric_non_numeric_evidence_ids'] = [e.evidence_id for e in evidence
                                                          if e not in candidates]
        extracted = {}
        for packet in packets:
            result = await self.dispatch(packet)
            packet_ids = {e['evidence_id'] for e in packet.data['evidence']}
            for item in result.requests:
                if item.name not in FORMULAS:
                    raise ValueError('unregistered_formula')
                if any(i.evidence_id not in packet_ids for i in item.inputs):
                    raise ValueError('metric_input_evidence')
                extracted.setdefault(item.name, []).append(item)
        merged = []
        for name, items in extracted.items():
            values, ambiguous = {}, len({i.applicable for i in items}) > 1
            for item in items:
                for value in item.inputs:
                    if value.name not in FORMULAS[name][:2]:
                        raise ValueError('metric_input_name')
                    if value.evidence_id not in self.coverage['metric_candidate_evidence_ids']:
                        raise ValueError('metric_input_evidence')
                    if value.value is None or value.extraction_status == 'missing':
                        continue
                    prior = values.get(value.name)
                    if prior and prior.model_dump(exclude={'evidence_id'}) != value.model_dump(exclude={'evidence_id'}):
                        ambiguous = True
                    values.setdefault(value.name, value)
            if ambiguous:
                self.metric_conflicts.append(name)
            merged.append({'name': name, 'inputs': [] if ambiguous else list(values.values()),
                           'applicable': True if ambiguous else items[0].applicable})
        return MetricExtraction(requests=merged)

    async def analyze(self, request, evidence, metrics):
        rows = self.evidence_rows(evidence)
        packets = pack_all(rows, lambda batch: self.prepare('analyst', {
            'request': request_only(request).model_dump(mode='json'), 'evidence': batch,
            'metrics': [m.model_dump(mode='json') for m in metrics]}), self.fits)
        self.require_calls(len(packets))
        self.coverage['analyst_evidence_ids'] = [e['evidence_id'] for e in rows]
        claims = []
        for number, packet in enumerate(packets, 1):
            result = await self.dispatch(packet)
            ids = [c.claim_id for c in result.claims]
            if len(ids) != len(set(ids)):
                raise ValueError('duplicate_draft_claim')
            claims.extend(c.model_copy(update={'claim_id': f'b{number}:{c.claim_id}'})
                          for c in result.claims)
        # No trimming of difficult/duplicate-looking claims to obtain a PASS.
        if len(claims) > 12:
            raise BudgetStop('CONTEXT_DRAFT_SCOPE_BLOCKED')
        return AnalystDraft(claims=claims)

    async def verify(self, claims, evidence, metrics):
        by_e = {e['evidence_id']: e for e in self.evidence_rows(evidence)}
        by_m = {m.metric_id: m.model_dump(mode='json') for m in metrics}
        all_claims = self.all_claims or claims
        def support(batch):
            mids = {mid for c in batch for mid in c.metric_ids}
            eids = {eid for c in batch for eid in c.evidence_ids}
            eids.update(i['evidence_id'] for mid in mids for i in by_m[mid]['inputs'])
            return self.prepare('verifier', {'claims': [c.model_dump(mode='json') for c in batch],
                'evidence': [by_e[eid] for eid in sorted(eids)],
                'metrics': [by_m[mid] for mid in sorted(mids)]})
        support_packets = pack_all(claims, support, self.fits)
        # Every consistency batch sees ALL original claims; every retrieved chunk is
        # swept once. This tests cross-batch relations, not just cited support.
        consistency_packets = pack_all(list(by_e.values()), lambda batch: self.prepare(
            'consistency', {'claims': [c.model_dump(mode='json') for c in all_claims],
                'evidence': batch, 'metrics': list(by_m.values())}), self.fits)
        self.require_calls(len(support_packets) + len(consistency_packets))
        decisions = {}
        for packet in support_packets:
            result = await self.dispatch(packet)
            expected = {c['claim_id'] for c in packet.data['claims']}
            received = [d.claim_id for d in result.decisions]
            if len(received) != len(set(received)) or set(received) != expected:
                raise ValueError('semantic_batch_ids_invalid')
            decisions.update({d.claim_id: d for d in result.decisions})
        if set(decisions) != {c.claim_id for c in claims}:
            raise ValueError('semantic_claim_coverage')
        for packet in consistency_packets:
            result = await self.dispatch(packet)
            expected = {c.claim_id for c in all_claims}
            if (len(result.reviewed_claim_ids) != len(set(result.reviewed_claim_ids)) or
                    set(result.reviewed_claim_ids) != expected):
                raise ValueError('consistency_claim_coverage')
            for finding in result.findings:
                if (not set(finding.claim_ids) <= expected or not set(finding.evidence_ids) <=
                        {e['evidence_id'] for e in packet.data['evidence']}):
                    raise ValueError('consistency_finding_scope')
                for cid in finding.claim_ids:
                    if cid not in decisions:  # Already hard-rejected; never promoted.
                        continue
                    old = decisions[cid]
                    if finding.kind == 'conflict':
                        old.conflicts.append(finding.explanation + ' Evidence: ' +
                                             ', '.join(finding.evidence_ids))
                    else:
                        old.overstatement = True
                        old.explanation += ' ' + finding.explanation
        self.coverage['verified_claim_ids'] = sorted(decisions)
        self.coverage['consistency_claim_ids'] = [c.claim_id for c in all_claims]
        self.coverage['consistency_evidence_ids'] = list(by_e)
        self.coverage['consistency_pair_count'] = len(all_claims) * len(by_e)
        self.coverage['verification_draft_hash'] = fingerprint([c.model_dump(mode='json') for c in claims])
        return SemanticBatch(decisions=list(decisions.values()))


class ContextWorkflow(P2Workflow):
    def hard_checks(self, claim, evidence, metrics, repository, cutoff):
        if self.adapter.execution.configuration().get('numeric_verification_version') == 'p2-metric-bound-display-v3':
            from app.numeric_display import inspect_display
            from app.verification_v3 import hard_checks
            if claim.metric_ids:
                self.adapter.coverage.setdefault('numeric_display_checks', {})[claim.claim_id] = inspect_display(claim, metrics)
        else:
            from app.verification import hard_checks
        return hard_checks(claim, evidence, metrics, repository, cutoff)

    async def model_call(self, state, method, *args):
        # Each subrequest retains a 30s timeout in paid_call. Quota waiting is outside
        # that timer and inside Ledger's overall deadline, including every sub-batch.
        self.adapter.ledger.check()
        try:
            return await method(*args)
        finally:
            state.budget.llm_calls = self.adapter.ledger.case_counts['chat']

    async def research(self, state):
        await super().research(state)
        return self.finalize_research(state)

    def finalize_research(self, state):
        """Same lossless local closure for live and attested successful checkpoints."""
        config = self.adapter.execution.configuration()
        complete = unique_evidence([Evidence.model_validate(raw)
            for event in state.tool_events if event.name == 'search_documents'
            and event.result.status == 'success' for raw in event.result.data])
        if config.get("evidence_policy"):
            from evaluation.evidence_policy import validate_events
            validate_events(state.tool_events, config)
        if len(complete) > config["max_retained_evidence"]:
            self.adapter.ledger.stop('CONTEXT_EVIDENCE_COUNT_BLOCKED')
        for evidence in complete:
            self.tools.repository.trace(evidence, state.request.as_of_date)
        state.evidence = complete  # retain later search results, including adverse sources
        self.adapter.coverage['retained_evidence_ids'] = [e.evidence_id for e in complete]
        return state.model_dump()

    async def metrics_node(self, state):
        await super().metrics_node(state)
        if self.adapter.metric_conflicts:
            state.gaps.append(Gap(code='context_metric_ambiguous',
                message='Conflicting extracted candidates retained; affected metrics have missing inputs.'))
        return state.model_dump()

    async def verify_claims(self, state, claims):
        # Rebuild all packets on every invocation; no decision cache can survive an edit.
        replacements = {c.claim_id: c for c in claims}
        self.adapter.all_claims = copy.deepcopy([
            replacements.get(c.claim_id, c) for c in state.draft_claims])
        if self.adapter.execution.configuration().get('numeric_verification_version') == 'p2-metric-bound-display-v3':
            from app.verification_v3 import verify_claims
            return await verify_claims(self, state, claims)
        return await super().verify_claims(state, claims)
