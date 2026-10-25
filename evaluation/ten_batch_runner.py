"""Sequential held-out execution. No evaluator imports and no paid retry/resume."""
import asyncio
import re
from datetime import UTC, datetime
from time import monotonic

from app.openai_responses import BASE_URL, MODEL, audit_wire
from evaluation.candidate_execution import _write
from evaluation.frozen_adapters import safe_error
from evaluation.frozen_batch import execute_case, seal_batch
from evaluation.frozen_budget import BudgetStop
from evaluation.openai_runtime import OpenAIContextAdapter
from evaluation.ten_batch_budget import TenExecution


class AuditedAdapter(OpenAIContextAdapter):
    def attach(self, ledger):
        self.ledger = ledger
        self.http_requests = 0
        self.identity = None

        async def sent(request):
            if str(request.url) != BASE_URL+'/responses':
                raise BudgetStop('unexpected_endpoint')
            self.http_requests += 1
            ledger.append('http_request', case_id=ledger.case_id, endpoint=str(request.url),
                          model=MODEL, automatic_retry=0, node=getattr(self, 'active_role', None))

        async def received(response):
            # Save available headers before waiting for a possibly slow response body.
            self.metadata(response)
            ledger.append('http_response_headers', case_id=ledger.case_id,
                          node=getattr(self, 'active_role', None), http_status=response.status_code,
                          request_id=self.last_request_id, metadata=self.last_response_metadata)
            await response.aread()
            try:
                body = response.json()
            except ValueError:
                body = {}
            error = body.get('error') or {}
            self.identity = {k:body.get(k) for k in ('id','model','status','service_tier')}
            ledger.append('http_response_identity', case_id=ledger.case_id,
                          http_status=response.status_code, **self.identity,
                          error_type=error.get('type'), error_code=error.get('code'))

        self.client._client.event_hooks.setdefault('request',[]).append(sent)
        self.client._client.event_hooks.setdefault('response',[]).append(received)

    async def request(self, wire, *, role, schema=None):
        self.identity = None
        self.active_role = role
        started_at = datetime.now(UTC).isoformat()
        self.ledger.append('request_wire', case_id=self.ledger.case_id, role=role,
                           wire=audit_wire(wire),
                           token_breakdown=self.counter.breakdown(wire,wire['max_output_tokens']))
        before, started, error = len(self.usage), monotonic(), None
        try:
            result = await super().request(wire,role=role,schema=schema)
            actual = (self.identity or {}).get('model')
            if actual != MODEL and not (isinstance(actual,str) and re.fullmatch(
                    re.escape(MODEL)+r'-\d{4}-\d{2}-\d{2}',actual)):
                raise BudgetStop('response_model_mismatch')
            return result
        except (Exception,asyncio.CancelledError) as exc:
            error = safe_error(exc)
            raise
        finally:
            self.ledger.append('adapter_response', case_id=self.ledger.case_id, role=role,
                elapsed_seconds=monotonic()-started, error=error,
                usage=self.usage[-1] if len(self.usage)==before+1 else None,
                request_id=self.last_request_id, metadata=self.last_response_metadata,
                response=self.last_output, started_at=started_at, ended_at=datetime.now(UTC).isoformat(),
                timeout_layer=getattr(self, 'last_timeout_layer', None),
                request_timeout_seconds=self.role_request_seconds.get(role, self.request_seconds))


async def execute_ten(*, root, manifest, directory, ledger, account, permit, adapter_factory,
                      embedding_factory, context_factory=TenExecution, prepare_context=None, continue_after_failure=None):
    """All accounting and observed rate windows survive the case boundary."""
    rows, shared_quota = [], None
    case_ids = manifest['batches']['heldout']
    for cid in case_ids:
        adapter, embedding = adapter_factory(), embedding_factory()
        context = context_factory(ledger,account)
        if prepare_context is not None:
            prepare_context(cid, context, adapter)
        if shared_quota is not None:
            context.quota = shared_quota
        shared_quota = context.quota
        try:
            result,_ = await execute_case(root/manifest['frozen_directory'],cid,root=root,
                batch_directory=directory,cache_directory=root/manifest['index_cache'],
                adapter=adapter,embedding=embedding,ledger=ledger,paid_permit=permit,context=context)
            row = {'case_id':cid, 'error':result.get('error'),
                   'arms':[{'arm':r['arm'],'status':r['output']['status'],
                            'claims':len(r['output']['claims']),
                            'execution_status':r.get('execution_status','executed')}
                           for r in result['records']],
                   'shared_hash_validation':result.get('shared_hash_validation'),
                   'chat_http_requests':getattr(adapter,'http_requests',None),
                   'embedding_http_requests':getattr(embedding,'http_request_attempts',None),
                   'cumulative_budget':ledger.summary()}
            rows.append(row)
            _write(directory/f'progress-{len(rows):02}.json',row)
            print(cid,row['error'] or 'SEALED',ledger.summary()['budget_committed_usd'],flush=True)
            if not result.get('error') and not ledger.halted:
                ledger.finish_case()
        finally:
            await adapter.close()
            await embedding.close()
        if ((result.get('error') or ledger.halted) and
                (continue_after_failure is None or not continue_after_failure(
                    cid, result, ledger, context, adapter, embedding))):
            break
    # Terminal partial batches may be scored only after all started outputs seal;
    # no later answering calls or automatic resume are permitted for this batch.
    completed_ids = [r['case_id'] for r in rows]
    if completed_ids:
        seal_batch(directory,completed_ids)
    status={'case_ids':case_ids,'started_case_ids':completed_ids,
            'not_started_case_ids':[c for c in case_ids if c not in completed_ids],
            'complete_heldout_denominator':10,'rows':rows,
            'status':('COMPLETED' if len(rows)==len(case_ids) and not any(r['error'] for r in rows)
                      else 'COMPLETED_WITH_FAILURES' if len(rows)==len(case_ids) else 'PARTIAL_STOPPED'),
            'stop_reason':ledger.halted,'budget':ledger.summary(),
            'automatic_resume':False,'automatic_retry':0}
    _write(directory/'EXECUTION_STATUS.json',status)
    return status
