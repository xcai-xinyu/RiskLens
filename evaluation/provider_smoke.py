import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.adapter import OpenAIAdapter
from app.schemas import ResearchRequest


async def smoke():
    if not os.environ.get('OPENAI_API_KEY') or not os.environ.get('LLM_MODEL'):
        print(json.dumps({'status': 'BLOCKED', 'mode': 'provider_smoke',
                          'reason': 'Required process-injected provider configuration missing'}))
        return 2
    # Do not enable external tracing of request data.
    os.environ['LANGSMITH_TRACING'] = 'false'
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    request = ResearchRequest(company='Example', question='Plan a liquidity evidence search',
        as_of_date='2025-01-01', risk_categories=['liquidity'], mode='offline_research')
    try:
        plan = await asyncio.wait_for(OpenAIAdapter().plan(request), timeout=30)
    except Exception:  # noqa: BLE001 - redact all errors at the untrusted adapter boundary
        print(json.dumps({'status': 'FAIL', 'mode': 'provider_smoke',
                          'reason': 'Provider timeout, call or structured validation failed'}))
        return 1
    print(json.dumps({'status': 'PASS', 'mode': 'provider_smoke',
                      'schema': type(plan).__name__, 'usage': None,
                      'usage_reason': 'not captured by isolated smoke'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(smoke()))
