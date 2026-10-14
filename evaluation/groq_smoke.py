"""Authorized chat-only Groq smoke: tool calling and strict Pydantic output separately."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.environment import load_environment
from app.providers import chat_configuration, make_chat_adapter
from app.schemas import ResearchRequest


async def smoke():
    adapter = make_chat_adapter(provider='groq', max_calls=2)
    request = ResearchRequest(company='Coinbase', question='Search local documents about liquidity.',
        as_of_date='2025-02-13', risk_categories=['liquidity'], mode='offline_research')
    report = {'provider': adapter.provider, 'model': adapter.model, 'simulated': False,
        'scope': 'CHAT ONLY; no embeddings, retrieval, trades or full RAG', 'sdk_max_retries': 0,
        'llm_call_limit': 2, 'total_timeout_seconds': 90, 'checks': {}}
    start = monotonic()
    try:
        turn = await adapter.research_turn({'request': request.model_dump(mode='json'),
            'company_id': 'COIN', 'plan': {'company_id': 'COIN', 'search_queries': ['liquidity']},
            'tool_events': [], 'instruction': 'Call search_documents exactly once for COIN with cutoff 2025-02-13. Do not answer without calling it.'})
        call = turn.tool_call
        from app.tools.research import SearchArguments
        args = SearchArguments.model_validate(call.arguments) if call and call.name == 'search_documents' else None
        passed = args is not None and args.company_id == 'COIN' and args.as_of_date == request.as_of_date
        report['checks']['tool_calling'] = {'status': 'PASS' if passed else 'FAIL',
            'action': turn.action, 'tool_name': call.name if call else None,
            'arguments_validated': passed, 'tool_executed': False}
    except Exception as error:  # noqa: BLE001 - never persist arbitrary provider error text
        report['checks']['tool_calling'] = {'status': 'BLOCKED', 'code': getattr(error, 'code', 'tool_contract_error')}
    try:
        plan = await adapter.make_plan(request, 'COIN')
        passed = plan.company_id == 'COIN'
        report['checks']['structured_output'] = {'status': 'PASS' if passed else 'FAIL',
            'schema': 'ResearchPlan', 'strict': True, 'validated': True,
            'plan': plan.model_dump(mode='json')}
    except Exception as error:  # noqa: BLE001 - never persist arbitrary provider error text
        report['checks']['structured_output'] = {'status': 'BLOCKED', 'code': getattr(error, 'code', 'structured_contract_error')}
    report.update(actual_usage=adapter.usage, provider_failures=adapter.failures,
        llm_calls=adapter.calls, latency_seconds=monotonic()-start, cost=None)
    report['status'] = 'PASS' if all(c['status']=='PASS' for c in report['checks'].values()) else 'BLOCKED'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    load_environment()
    os.environ['LANGSMITH_TRACING'] = 'false'
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    _, _, model_name, missing = chat_configuration('groq')
    target = Path(args.output)
    if target.exists():
        raise SystemExit('immutable_smoke_output_exists')
    if missing:
        result = {'status':'BLOCKED', 'code':'provider_configuration_missing', 'missing':missing,
            'key_present':bool(os.environ.get('GROQ_API_KEY')), 'provider':'groq',
            'model':os.environ.get(model_name), 'simulated':False}
    elif not args.worker:
        from evaluation.p4_process import bounded
        code = bounded([sys.executable, str(Path(__file__).resolve()), '--output', str(target), '--worker'], 92)
        if code == 124 and not target.exists():
            with target.open('x') as f:
                json.dump({'status':'BLOCKED','code':'smoke_hard_timeout','worker_group_terminated':True,'simulated':False}, f)
        return code
    else:
        try:
            result = asyncio.run(asyncio.wait_for(smoke(), timeout=90))
        except TimeoutError:
            result = {'status':'BLOCKED','code':'smoke_total_timeout','simulated':False}
    with target.open('x') as f:
        json.dump(result, f, indent=2)
    print(json.dumps({'status':result['status'],'provider':'groq','output':str(target)}))
    return 0 if result['status']=='PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
