"""History comparison uses persisted coverage/version snapshots; never invents trends."""


def compare_reports(left,right):
    def descriptor(report):
        m=report.metadata
        return {'run_id':report.run_id,'company':report.company,'as_of_date':str(report.as_of_date),
            'status':report.status,'overall_risk':report.overall_risk,'mode':m.get('mode'),
            'simulated':m.get('simulated'),'corpus_version':m.get('corpus_version'),
            'corpus_hash':m.get('corpus_hash'),'corpus_manifest_hash':m.get('corpus_manifest_hash'),
            'sources':[{'document_id':d.document_id,'title':d.title,'published_at':str(d.published_at),
                        'content_hash':d.content_hash} for d in report.sources],
            'model':m.get('model'),'provider':m.get('provider'),
            'prompt_version':m.get('prompt_version'),'prompt_hashes':m.get('prompt_hashes'),
            'rubric_version':m.get('rubric_version'),'formula_version':m.get('formula_version'),
            'verification_version':m.get('verification_version'),
            'p2_content_hashes':m.get('p2_content_hashes')}
    a,b=descriptor(left),descriptor(right)
    reasons=[]
    for field in ('company','mode','simulated','corpus_version','corpus_hash','corpus_manifest_hash',
                  'sources','model','provider','prompt_version','prompt_hashes','rubric_version',
                  'formula_version','verification_version','p2_content_hashes'):
        if a[field]!=b[field]:
            reasons.append(field+'_differs')
    for field in ('corpus_hash','model','prompt_version','rubric_version'):
        if a[field] is None or b[field] is None:
            reasons.append(field+'_unknown')
    if left.status!='complete' or right.status!='complete':
        reasons.append('incomplete_research_coverage')
    return {'left':a,'right':b,'directly_comparable':not reasons,'reasons':reasons,
        'notice':'Comparable snapshots; review observed values in context.' if not reasons else
                 'Not directly comparable: coverage or execution versions differ or remain incomplete.',
        'risk_change_conclusion':None,
        'observed_risk_factors':{'left':[f.model_dump(mode='json') for f in left.risk_factors],
                                 'right':[f.model_dump(mode='json') for f in right.risk_factors]},
        'observed_metrics':{'left':[m.model_dump(mode='json') for m in left.metrics],
                            'right':[m.model_dump(mode='json') for m in right.metrics]}}
