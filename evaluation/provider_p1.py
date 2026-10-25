"""Opt-in real offline P1 check; never substitutes demo. Does not print source text or keys."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app.schemas import ResearchRequest
from app.service import ResearchService


async def check():
    missing=[k for k in ('OPENAI_API_KEY','LLM_MODEL','EMBEDDING_MODEL') if not os.environ.get(k)]
    if missing:
        print(json.dumps({'status':'BLOCKED','mode':'offline_research','missing_configuration':missing}))
        return 2
    request=ResearchRequest(company='Coinbase',question='Analyze liquidity resources and regulatory SEC appeal.',
        as_of_date='2025-02-13',risk_categories=['liquidity','regulatory'],mode='offline_research')
    try:
        state=await ResearchService().research(request)
    except Exception:  # noqa: BLE001 - redacted real provider test boundary
        print(json.dumps({'status':'FAIL','reason':'P1 real provider/embedding call failed'}))
        return 1
    print(json.dumps({'status':'FAIL' if state.errors else 'PASS','mode':'offline_research',
        'simulated':False,'run_id':state.run_id,'model_calls':state.budget.llm_calls,
        'tool_calls':state.budget.tool_calls,'draft_claim_ids':[c.claim_id for c in state.draft_claims],
        'errors':[e.code for e in state.errors]}))
    return 1 if state.errors or not state.draft_claims else 0


if __name__=='__main__':
    raise SystemExit(asyncio.run(check()))
