"""Answer-blind, deterministic stratified selection and complete-batch capacity check."""
import hashlib
from decimal import Decimal

from evaluation.candidate_data import fingerprint
from evaluation.frozen_budget import BudgetStop
from evaluation.openai_runtime import admit_plan

VERSION = 'msft-wmt-ten-v1'
SEED = '20261023'
CATEGORIES = ('liquidity', 'credit', 'market', 'operational', 'regulatory')


def select_ten(metadata, *, seed=SEED):
    """Input whitelist excludes question, Gold, answers, difficulty and scores."""
    fields = {'case_id','company','risk_category','document_ids','document_groups','split','company_group'}
    if any(set(row) != fields for row in metadata):
        raise ValueError('selection_metadata_only')
    if len({r['case_id'] for r in metadata}) != len(metadata):
        raise ValueError('duplicate_case_id')
    selected = []
    for company in ('Microsoft','Walmart'):
        for category in CATEGORIES:
            pool = sorted([r for r in metadata if r['company'] == company and
                           r['risk_category'] == category and r['split'] == 'heldout'],
                          key=lambda r:r['case_id'])
            if not pool:
                raise ValueError('missing_stratum:'+company+':'+category)
            def rank(row, company=company, category=category):
                return hashlib.sha256(f'{seed}|{company}|{category}|{row["case_id"]}'.encode()).hexdigest()
            winner = min(pool, key=rank)
            selected.append({**winner, 'selection_hash': rank(winner),
                             'eligible_ids': [r['case_id'] for r in pool]})
    return {'version':VERSION,'seed':seed,
        'algorithm':'Minimum SHA256(seed|company|primary_risk_category|case_id); UTF-8; lexicographic digest order',
        'selection_inputs_hash':fingerprint(metadata),'selected':selected,
        'reserve_ids':sorted(r['case_id'] for r in metadata if r['case_id'] not in
                             {s['case_id'] for s in selected} and r['split'] == 'heldout'),
        'gold_used':False, 'complete_heldout_size':10,'previous_planned_size':20}


def batch_capacity(plans, limits, account, *, usd_cap=Decimal(6)):
    """Retain current full-case cost rule; share identical corpus indexing once.

    Capacity reservation is NOT a prediction of actual spend. It is kept distinct
    from measured development usage. Failure happens before any paid A call.
    """
    seen, rows, failures = set(), [], []
    for source in plans:
        plan = dict(source)
        shared = plan['config_hash'] in seen
        if shared:
            plan.update(index_requests=0, index_input_token_upper_bound=0, index_batches=[])
        seen.add(plan['config_hash'])
        try:
            row = admit_plan(plan, limits, account)
        except BudgetStop as error:
            failures.append({'case_id':plan['case_id'],'reason':str(error)})
            # Retain independently calculable bounds even when another cap blocks.
            chat = limits.case_chat_calls*(limits.chat_input_tokens*limits.input_usd_per_million+
                                           limits.chat_output_tokens*limits.output_usd_per_million)/1_000_000
            embed = (plan['index_input_token_upper_bound']+plan['query_input_token_upper_bound'])*limits.embedding_usd_per_million/1_000_000
            row = dict(plan, chat_estimated_upper_usd=str(chat), embedding_estimated_upper_usd=str(embed),
                       total_estimated_upper_usd=str(chat+embed),status='BLOCKED')
        rows.append(dict(row, index_shared_with_preceding_case=shared))
    total = sum((Decimal(r['total_estimated_upper_usd']) for r in rows), Decimal(0))
    return {'version':VERSION,'case_count':len(plans),'cases':rows,
        'required_capacity_usd':str(total),'authorized_cap_usd':str(usd_cap),
        'capacity_gap_usd':str(max(Decimal(0),total-usd_cap)),
        'status':'BLOCKED' if failures or total>usd_cap else 'PASS',
        'failures':failures,'budget_rule':'Unchanged openai_runtime.admit_plan upper envelope per case; one index per exact corpus config',
        'actual_cost':None,'actual_requests':0,'cost_is_forecast':False}
