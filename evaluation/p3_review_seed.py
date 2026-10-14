"""Explicit opt-in H3 synthetic history seed; never part of production startup."""
import argparse
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))

from app.schemas import ResearchRequest, RiskReport
from app.storage.run_repository import RunRepository


def seed(path):
    report=RiskReport.model_validate_json((ROOT/'evaluation/audit/P2/attempt-002/artifacts/examples/synthetic_report.json').read_text())
    request=ResearchRequest(company='Coinbase',question='H3 synthetic review history',as_of_date=report.as_of_date,risk_categories=['liquidity'],mode='demo')
    repo=RunRepository(path)
    try:
        for run_id,date,changed in [('h3-synthetic-a','2025-02-13',False),('h3-synthetic-b','2025-03-01',True)]:
            if repo.get(run_id):
                continue  # immutable seed, no replacement
            item=report.model_copy(deep=True)
            item.run_id=run_id;item.as_of_date=date
            item.metadata=dict(item.metadata,h3_review_fixture=True,label='H3 SYNTHETIC HISTORY FIXTURE / SIMULATED VERIFICATION')
            if changed:
                item.sources=[]
                item.claims=[];item.summary=''
                item.risk_factors=[f.model_copy(update={'verified_claim_ids':[]}) for f in item.risk_factors]
                item.metadata=dict(item.metadata,corpus_version='h3-synthetic-reduced-v1',corpus_hash='h3-reduced-coverage-fixture',model='h3-other-fixture-model',citations={},claim_coverage={'original':5,'supported_final':0})
            repo.create(run_id,request.model_copy(update={'as_of_date':item.as_of_date}),{})
            repo.finish(run_id,item,'partial',[],{})
    finally:
        repo.close()
    return ['h3-synthetic-a','h3-synthetic-b']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    args=parser.parse_args()
    print('Explicit SYNTHETIC H3 fixtures seeded:',seed(args.database))
