"""Freeze P4 engineering cases once. Annotations are proposals for H4, not human approval."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
DATA=ROOT/'evaluation/datasets/p4-v1'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    if (DATA/"manifest.json").exists():
        raise FileExistsError("Frozen dataset exists; publish a new version instead")
    # Existing approved demo trace binds real-source evidence; gold below references text,
    # never imports verification_results/status as the evaluator's truth.
    report=json.loads((ROOT/'evaluation/audit/P3/attempt-001/artifacts/runtime-local-network/http-report.json').read_text())
    citations=report['metadata']['citations'];claims=report['claims']
    snapshots=[];cases=[];annotations=[]
    for n in range(10):
        chosen=claims[n%len(claims)]
        eid=chosen['evidence_ids'][0];ev=citations[eid].copy();ev.pop('number',None)
        request={'company':'Coinbase','question':chosen['text'],'as_of_date':'2025-02-13','risk_categories':[chosen['risk_category']],'mode':'demo'}
        # Freeze the original draft + evidence, not a new quality evaluation of the model.
        snapshot={'id':f'dev-{n:02}','request':request,'corpus':'data/sample/corpus_v1.json',
            'evidence':[ev],'baseline':[chosen],'metrics':[],
            'provenance':{'label':'REAL DOCUMENT / REPLAYED SIMULATED ANALYST','prior_run':report['run_id']}}
        snapshots.append(snapshot)
        cases.append({'case_id':snapshot['id'],'split':'development','company_group':'COIN',
            'document_group':'coinbase-q424','question':request['question'],'company':request['company'],
            'as_of_date':request['as_of_date'],'document_ids':[ev['document_id']],
            'gold_evidence_ids':[eid],'reference_claims':[{'text':chosen['text'],'claim_type':chosen['claim_type'],'evidence_ids':[eid]}],
            'reference_metrics':[],'expected_gaps':[],'expected_abstention':False,
            'judgments':[{'text':chosen['text'],'verdict':'supported','evidence_ids':[eid]}],
            'snapshot_id':snapshot['id'],'annotation_status':'PROPOSED_H4','source_kind':'real_fixed_pdf'})
        annotations.append({'case_id':snapshot['id'],'author':'agent','status':'PROPOSED_H4',
            'basis':'Original quote in fixed real PDF; candidate text comes from earlier human-reviewed demo claim. No Verifier status imported as gold.',
            'locations':[{'evidence_id':eid,'page':ev['page'],'section':ev['section'],'quote':ev['quote']}],
            'human_review':None})
    # Held-out isolation is by NEW fictitious company/document group, never regression fixtures.
    # These cases validate the evaluator/ablation mechanism, not live financial model quality.
    for n in range(20):
        company='Aster Fixture' if n<10 else 'Birch Fixture'
        group='ASTER_SYNTHETIC' if n<10 else 'BIRCH_SYNTHETIC'
        raw=f'Cash is {20+n} million USD FY:2024. current_assets {200+10*n} million USD FY:2024; current_liabilities 100 million USD FY:2024. This synthetic source does not establish guaranteed safety.\n'
        source=DATA/f'heldout-{n:02}.txt';source.write_text(raw)
        corpus={'version':'p4-heldout-synthetic-v1','company_aliases':{company.lower():group},'documents':[
            {'path':source.name,'company_id':group,'title':f'SYNTHETIC {company} disclosure {n}',
             'url':f'https://example.com/p4-fixture/{n}','published_at':'2025-01-01','source_group_id':group,
             'sha256':sha(source)}],'historical_limitation':'Entire source is synthetic engineering data.'}
        corpus_path=DATA/f'heldout-{n:02}.json';corpus_path.write_text(json.dumps(corpus,indent=2))
        # IDs/locations come from the actual unchanged parser/Repository, not invented citations.
        import tempfile

        from app.p1_contracts import CorpusEntry
        from app.rag.ingestion import ingest_file
        from app.storage.repository import Repository
        with tempfile.TemporaryDirectory(prefix='p4-freeze-') as directory:
            repo=Repository(directory)
            result=ingest_file(repo,DATA,CorpusEntry.model_validate(corpus['documents'][0]));assert result.status=='success'
            chunk=repo.chunks()[0]
            ev={'evidence_id':'ev-'+chunk.chunk_id.removeprefix('chunk-'),'document_id':chunk.document_id,
                'chunk_id':chunk.chunk_id,'quote':chunk.text,'page':chunk.page,'section':chunk.section,
                'relevance':1,'source_group_id':chunk.source_group_id}
            repo.close()
        amount=20+n;normal=f'Cash is {amount} million USD FY:2024.'
        kind=n%5
        text=[normal,normal+' Liquidity is guaranteed safe.',
              f'Cash is {amount+500} million USD FY:2024.',normal,normal][kind]
        verdict=['supported','partially_supported','contradicted','unsupported','unverifiable'][kind]
        c={'claim_id':f'heldout-c{n}','text':text,'claim_type':'fact','risk_category':'liquidity',
            'evidence_ids':['fabricated-evidence'] if kind==3 else [ev['evidence_id']],'metric_ids':[]}
        # Date-leak case preserves a deliberately invalid frozen Analyst input for hard ablation.
        cutoff='2024-12-31' if kind==4 else '2025-02-13'
        request={'company':company,'question':'Assess disclosed cash and liquidity safety','as_of_date':cutoff,
                 'risk_categories':['liquidity'],'mode':'demo'}
        item={'id':f'heldout-{n:02}','request':request,'corpus':str(corpus_path.relative_to(ROOT)),
              'evidence':[ev],'baseline':[c],'metrics':[],
              'provenance':{'label':'SYNTHETIC ADVERSARIAL SNAPSHOT / SIMULATED ANALYST','adversarial_ineligible_evidence':kind==4}}
        snapshots.append(item)
        allowed=[ev['evidence_id']] if kind not in (3,4) else []
        judgments=[{'text':text,'verdict':verdict,'evidence_ids':allowed}]
        if kind==1:judgments.append({'text':normal,'verdict':'supported','evidence_ids':[ev['evidence_id']]})
        cases.append({'case_id':item['id'],'split':'heldout','company_group':group,'document_group':group,
            'question':request['question'],'company':company,'as_of_date':cutoff,'document_ids':[ev['document_id']],
            'gold_evidence_ids':allowed,'reference_claims':[{'text':normal,'claim_type':'fact','evidence_ids':[ev['evidence_id']]}] if kind<3 else [],
            'reference_metrics':[],'expected_gaps':['after_cutoff'] if kind==4 else [],
            'expected_abstention':kind in (3,4),'judgments':judgments,'snapshot_id':item['id'],
            'annotation_status':'PROPOSED_H4','source_kind':'synthetic'})
        annotations.append({'case_id':item['id'],'author':'agent','status':'PROPOSED_H4','human_review':None,
            'basis':'Independent source-text annotation proposal; exact amount, absent safety entailment, invalid ID or publication cutoff.',
            'locations':[{'evidence_id':ev['evidence_id'],'section':ev['section'],'quote':raw}],
            'limitations':'Synthetic withheld company groups; not a real-company held-out research benchmark.'})
    for name,items in [('dataset.jsonl',cases),('snapshots.jsonl',snapshots),('annotations.jsonl',annotations)]:
        with (DATA/name).open('x') as stream:
            for item in items:stream.write(json.dumps(item,ensure_ascii=False)+'\n')
    # Simulated decisions are a separate response fixture, explicitly NOT gold.
    responses={}
    for snapshot in snapshots:
        claim=snapshot['baseline'][0]
        if snapshot['id'].startswith('dev'):
            status='supported';over=False;narrow=None
        else:
            kind=int(snapshot['id'].split('-')[1])%5
            status=['supported','partially_supported','contradicted','supported','supported'][kind]
            over=kind==1;narrow=claim['text'].split(' Liquidity')[0] if over else None
        responses[snapshot['id']]={'status':status,'overstatement':over,'narrowed':narrow}
    with (DATA/'model_responses.json').open('x') as f:json.dump(responses,f,indent=2)
    paths=sorted(p for p in DATA.iterdir() if p.is_file())+[ROOT/'evaluation/prompts/raw_p4_v1.txt',ROOT/'data/sample/corpus_v1.json',ROOT/'data/sample/coinbase-q424.pdf']
    manifest={'version':'p4-v1','frozen_at':'2026-10-10','annotation_status':'PROPOSED_H4','human_approved':False,
        'development':10,'heldout':20,'split_unit':'company/document group','labels':'demo/synthetic engineering only',
        'arms':{'A':'raw model, no corpus','B':'frozen Research/Analyst draft, no filtering/repair','C':'B + hard + semantic verification, no repair','D':'B + hard only, no semantic verification'},
        'repair_protocol':'Separate paired C no-repair vs C with max 1 repair round; identical starting snapshot',
        'scorer_version':'p4-exact-reference-v1','gold_independent_of_verifier':True,
        'randomness':None,'budget':{'max_llm_calls':10,'max_tool_calls':12,'max_repair_rounds':1,'run_timeout_seconds':180},
        'files':{str(p.relative_to(ROOT)):sha(p) for p in paths},
        'model':'p4-fixture-v1','real_model_quality':'BLOCKED; must separately freeze model/config and run real provider',
        'limitations':['Ten development questions share one real company/document; twenty heldout cases are synthetic.',
                       'Repeated development reference facts are not independent statistical samples.',
                       'Heldout fixtures authored before freezing; no post-freeze tuning. H4 must review candidate labels.',
                       'A demo raw response is explicit abstention, not a real LLM evaluation.']}
    with (DATA/'manifest.json').open('x') as f:json.dump(manifest,f,indent=2)
    print(json.dumps({'cases':len(cases),'status':'FROZEN_CANDIDATE_LABELS','real_provider':'BLOCKED'}))


if __name__=='__main__':
    build()
